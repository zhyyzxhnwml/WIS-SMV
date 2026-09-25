#!/usr/bin/env python3
"""
实验数据提取 + 可视化生成器
============================
从实验结果的 CSV 文件中提取数据，生成：
1. 综合汇总 CSV 文件
2. 多张对比可视化图表
"""
import csv  # CSV文件读写
import os  # 操作系统接口
import sys  # 系统参数与标准流
import io  # IO流处理（含编码转换）
from pathlib import Path  # 跨平台路径操作
from collections import Counter, defaultdict  # 计数器与默认字典

# Windows terminal UTF-8
if sys.platform == "win32":  # 如果是Windows系统
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")  # 设置标准输出为UTF-8编码

import numpy as np  # 数值计算库
import pandas as pd  # 数据分析库
import matplotlib  # 绘图库
matplotlib.use('Agg')  # 使用非交互式后端（无GUI）
import matplotlib.pyplot as plt  # pyplot绘图接口
import matplotlib.ticker as mticker  # 坐标轴刻度格式化

# ── 配置 ──
# 脚本所在目录
BASE_DIR = Path(__file__).resolve().parent  # 脚本所在目录的绝对路径
RESULT_DIR = BASE_DIR / "results" / "run_1784460360"  # 实验结果目录
OUTPUT_DIR = RESULT_DIR  # 输出目录
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)  # 确保输出目录存在

# matplotlib 全局样式
plt.rcParams.update({  # 更新matplotlib全局参数
    'font.family': 'sans-serif',  # 字体族
    'font.sans-serif': ['Microsoft YaHei', 'SimHei', 'DejaVu Sans'],  # 中文字体候选列表
    'font.size': 12,  # 基础字号
    'axes.unicode_minus': False,  # 正确显示负号
    'figure.dpi': 150,  # 图形显示分辨率
    'savefig.dpi': 300,  # 保存图片分辨率
    'savefig.bbox': 'tight',  # 保存时紧贴内容
    'figure.figsize': (10, 6),  # 默认图形尺寸（宽x高，英寸）
})

# 调色板
C_AGENT = '#E74C3C'      # 红色 - 原始 Agent
C_CASCADE = '#2980B9'    # 蓝色 - 三级架构
C_AGENT_LIGHT = '#F1948A'  # 浅红色 - Agent辅助色
C_CASCADE_LIGHT = '#85C1E9'  # 浅蓝色 - Cascade辅助色
C_AGENT_DIM = '#FADBD8'  # 淡红色 - Agent填充色
C_CASCADE_DIM = '#D4E6F1'  # 淡蓝色 - Cascade填充色

RISK_COLORS = {'low': '#2ECC71', 'middle': '#F39C12', 'high': '#E74C3C'}  # 风险等级→颜色映射
ROUTING_COLORS = {  # 路由类别→颜色映射
    'normal': '#2ECC71',  # 正常（绿色）
    'needs_review': '#F39C12',  # 需审查（橙色）
    'obvious_issue': '#E74C3C',  # 明显问题（红色）
    'has_risk': '#9B59B6',  # 有风险（紫色）
}

# ──────────────────────────────────────────────
# 1. 数据提取
# ──────────────────────────────────────────────

def read_csv_safe(path):  # path: CSV文件路径
    """安全的 CSV 读取"""
    try:
        with open(path, encoding='utf-8-sig') as f:  # 打开CSV文件（含BOM头）
            return list(csv.DictReader(f))  # 返回字典行列表
    except Exception as e:  # 捕获所有异常
        print(f"  ⚠ 读取失败 {path.name}: {e}")
        return []  # 返回空列表

def classify_experiment(dirname: str) -> dict:  # dirname: 实验目录名
    """解析目录名获取实验元信息"""
    parts = dirname.split('_')  # 按下划线分割目录名
    info = {'dirname': dirname}  # 实验元信息字典

    if dirname.startswith('speed_agent'):  # 速度测试-Agent
        info['experiment'] = 'speed'  # 实验类型：速度
        info['system'] = 'agent'  # 系统：原始Agent
        info['ships'] = int(parts[2])  # 船舶数量
    elif dirname.startswith('speed_cascade'):  # 速度测试-Cascade
        info['experiment'] = 'speed'
        info['system'] = 'cascade'
        info['ships'] = int(parts[2])
    elif dirname.startswith('scaling_agent'):  # 扩展性测试-Agent
        info['experiment'] = 'scaling'
        info['system'] = 'agent'
        info['ships'] = int(parts[2])
    elif dirname.startswith('scaling_cascade'):  # 扩展性测试-Cascade
        info['experiment'] = 'scaling'
        info['system'] = 'cascade'
        info['ships'] = int(parts[2])
    elif dirname.startswith('capacity_agent'):  # 容量测试-Agent
        info['experiment'] = 'capacity'
        info['system'] = 'agent'
        info['ships'] = int(parts[2])
    elif dirname.startswith('capacity_cascade'):  # 容量测试-Cascade
        info['experiment'] = 'capacity'
        info['system'] = 'cascade'
        info['ships'] = int(parts[2])
    elif dirname.startswith('fault_nollm_agent'):  # 故障测试(LLM不可用)-Agent
        info['experiment'] = 'fault_nollm'
        info['system'] = 'agent'
        info['trial'] = int(parts[3])  # 试验编号
    elif dirname.startswith('fault_nollm_cascade'):  # 故障测试(LLM不可用)-Cascade
        info['experiment'] = 'fault_nollm'
        info['system'] = 'cascade'
        info['trial'] = int(parts[3])
    elif dirname.startswith('fault_nomodel_agent'):  # 故障测试(模型缺失)-Agent
        info['experiment'] = 'fault_nomodel'
        info['system'] = 'agent'
        info['trial'] = int(parts[3] if len(parts) > 3 else 0)  # 试验编号（容错处理）
    elif dirname.startswith('fault_nomodel_cascade'):  # 故障测试(模型缺失)-Cascade
        info['experiment'] = 'fault_nomodel'
        info['system'] = 'cascade'
        info['trial'] = int(parts[3] if len(parts) > 3 else 0)
    else:
        info['experiment'] = 'unknown'  # 未知实验类型
    return info

def extract_metrics(rows):  # rows: CSV行数据列表
    """从结果行中提取指标"""
    if not rows:  # 如果没有数据
        return {}  # 返回空字典

    violations = [r.get('violation_detected', 'False') for r in rows]  # 违规检测结果列表
    risks = [r.get('risk_level', 'unknown') for r in rows]  # 风险等级列表
    confidences = [float(r.get('confidence', '0').rstrip('%')) for r in rows]  # 置信度列表（去除百分号）

    # Cascade 特有: routing
    routings = [r.get('routing', '') for r in rows if r.get('routing')]  # 路由分配列表

    risk_counts = Counter(risks)  # 各风险等级计数
    violation_count = sum(1 for v in violations if v == 'True')  # 违规数量

    return {
        'total_ships': len(rows),  # 总船舶数
        'violation_count': violation_count,  # 违规数
        'violation_rate': violation_count / len(rows) if rows else 0,  # 违规率
        'risk_low': risk_counts.get('low', 0),  # 低风险数量
        'risk_middle': risk_counts.get('middle', 0),  # 中风险数量
        'risk_high': risk_counts.get('high', 0),  # 高风险数量
        'risk_low_pct': risk_counts.get('low', 0) / len(rows) * 100,  # 低风险占比(%)
        'risk_middle_pct': risk_counts.get('middle', 0) / len(rows) * 100,  # 中风险占比(%)
        'risk_high_pct': risk_counts.get('high', 0) / len(rows) * 100,  # 高风险占比(%)
        'confidence_mean': float(np.mean(confidences)) if confidences else 0,  # 平均置信度
        'confidence_std': float(np.std(confidences)) if confidences else 0,  # 置信度标准差
        'routings': routings,  # 路由分配原始列表
        'routing_counts': dict(Counter(routings)),  # 路由分配计数
    }

# ── 扫描所有实验结果 ──
print("=" * 60)
print("  实验数据提取")
print("=" * 60)

all_data = []  # 所有实验数据的汇总列表
speed_data = {'agent': None, 'cascade': None}  # 速度测试数据（各系统类型）
scaling_data = {'agent': {}, 'cascade': {}}  # 扩展性测试数据（按船舶数索引）
capacity_data = {'agent': {}, 'cascade': {}}  # 容量测试数据（按船舶数索引）
fault_data = defaultdict(lambda: {'agent': [], 'cascade': []})  # 故障测试数据（默认字典，各试验列表）

for exp_dir in sorted(RESULT_DIR.iterdir()):  # 遍历排序后的实验目录
    if not exp_dir.is_dir():  # 跳过非目录项
        continue

    info = classify_experiment(exp_dir.name)  # 解析目录名获取实验元信息
    csv_files = list(exp_dir.glob("results_*.csv"))  # 查找结果CSV文件
    if not csv_files:  # 如果没有CSV文件则跳过
        continue

    rows = read_csv_safe(csv_files[0])  # 安全读取第一个CSV文件
    metrics = extract_metrics(rows)  # 提取指标
    if not metrics:  # 指标为空则跳过
        continue

    info.update(metrics)  # 合并实验信息与指标
    all_data.append(info)  # 添加到汇总列表

    # 分类存储
    exp = info['experiment']  # 实验类型
    sys_name = info['system']  # 系统类型（agent/cascade）
    ships = info.get('ships', 0)  # 船舶数量

    if exp == 'speed':  # 速度测试
        speed_data[sys_name] = metrics  # 直接存储
    elif exp == 'scaling':  # 扩展性测试
        scaling_data[sys_name][ships] = metrics  # 按船舶数存储
    elif exp == 'capacity':  # 容量测试
        capacity_data[sys_name][ships] = metrics  # 按船舶数存储
    elif exp in ('fault_nollm', 'fault_nomodel'):  # 故障测试
        fault_data[exp][sys_name].append(metrics)  # 追加到试验列表

# ──────────────────────────────────────────────
# 2. 写入汇总 CSV
# ──────────────────────────────────────────────

print("\n[2/7] 生成汇总 CSV...")

csv_rows = []  # CSV数据行列表
for d in all_data:  # 遍历所有实验数据
    csv_rows.append({  # 添加一行汇总数据
        '实验类型': d['experiment'],  # 实验类型
        '系统': d['system'],  # 系统（agent/cascade）
        '船舶数': d.get('ships', ''),  # 船舶数量
        '试验号': d.get('trial', ''),  # 试验编号
        '总船舶数': d['total_ships'],  # 总船舶数
        '违规数': d['violation_count'],  # 违规数量
        '违规率': f"{d['violation_rate']:.1%}",  # 违规率（百分比格式）
        '低风险': d['risk_low'],  # 低风险数
        '中风险': d['risk_middle'],  # 中风险数
        '高风险': d['risk_high'],  # 高风险数
        '低风险%': f"{d['risk_low_pct']:.1f}%",  # 低风险占比
        '中风险%': f"{d['risk_middle_pct']:.1f}%",  # 中风险占比
        '高风险%': f"{d['risk_high_pct']:.1f}%",  # 高风险占比
        '平均置信度': f"{d['confidence_mean']:.3f}",  # 平均置信度
        '置信度标准差': f"{d['confidence_std']:.3f}",  # 置信度标准差
    })

df_summary = pd.DataFrame(csv_rows)  # 创建DataFrame
df_summary.to_csv(OUTPUT_DIR / "experiment_summary.csv", index=False, encoding='utf-8-sig')  # 写入CSV
print(f"  [OK] 汇总 CSV: {OUTPUT_DIR / 'experiment_summary.csv'}")
print(f"  [DATA] 共 {len(csv_rows)} 条实验记录")

# ──────────────────────────────────────────────
# 3. 图表 1: 扩展性 - 违规率对比
# ──────────────────────────────────────────────

print("\n[3/7] 生成扩展性违规率图...")

fig, axes = plt.subplots(1, 2, figsize=(14, 6))  # 创建1行2列的子图

# 左边: 原始 Agent
ax = axes[0]  # 左侧子图
if scaling_data['agent']:  # 如果有Agent数据
    sizes = sorted(scaling_data['agent'].keys())  # 各规模的船舶数（排序）
    violation_rates = [scaling_data['agent'][s]['violation_rate'] * 100 for s in sizes]  # 各规模违规率
    ax.plot(sizes, violation_rates, 'o-', color=C_AGENT, linewidth=2.5, markersize=8, label='Agent')  # 绘制折线图
    ax.set_title('原始 Agent 违规率随数据规模变化', fontsize=13, fontweight='bold', pad=15)  # 设置标题
    ax.set_xlabel('船舶数量', fontsize=12)  # X轴标签
    ax.set_ylabel('违规率 (%)', fontsize=12)  # Y轴标签
    for s, v in zip(sizes, violation_rates):  # 在每个数据点上标注数值
        ax.annotate(f'{v:.1f}%', (s, v), textcoords="offset points",
                    xytext=(0, 12), ha='center', fontsize=10, color=C_AGENT)
    ax.grid(True, alpha=0.3)  # 显示网格
    ax.set_xlim(0, max(sizes) * 1.15)  # 设置X轴范围

# 右边: Cascade
ax = axes[1]  # 右侧子图
if scaling_data['cascade']:  # 如果有Cascade数据
    sizes = sorted(scaling_data['cascade'].keys())  # 各规模的船舶数（排序）
    violation_rates = [scaling_data['cascade'][s]['violation_rate'] * 100 for s in sizes]  # 各规模违规率
    ax.plot(sizes, violation_rates, 's-', color=C_CASCADE, linewidth=2.5, markersize=8, label='Cascade')
    ax.set_title('三级架构违规率随数据规模变化', fontsize=13, fontweight='bold', pad=15)
    ax.set_xlabel('船舶数量', fontsize=12)
    ax.set_ylabel('违规率 (%)', fontsize=12)
    for s, v in zip(sizes, violation_rates):  # 在每个数据点上标注数值
        ax.annotate(f'{v:.1f}%', (s, v), textcoords="offset points",
                    xytext=(0, 12), ha='center', fontsize=10, color=C_CASCADE)
    ax.grid(True, alpha=0.3)
    ax.set_xlim(0, max(sizes) * 1.15)

plt.tight_layout()  # 自动调整子图间距
plt.savefig(OUTPUT_DIR / "chart01_scaling_violation_rate.png")  # 保存图片
plt.close()  # 关闭图形
print(f"  [OK] chart01_scaling_violation_rate.png")


# ──────────────────────────────────────────────
# 4. 图表 2: 扩展性 - 风险分布堆叠柱状图
# ──────────────────────────────────────────────

print("\n[4/7] 生成扩展性风险分布图...")

fig, axes = plt.subplots(1, 2, figsize=(16, 6), sharey=True)  # 创建1行2列子图，共享Y轴

for idx, (sys_name, sys_color, ax) in enumerate(zip(  # 遍历两种系统
    ['agent', 'cascade'], ['Agent', 'Cascade'], axes)):

    data = scaling_data[sys_name]  # 当前系统的扩展性数据
    if not data:  # 无数据则跳过
        continue
    sizes = sorted(data.keys())  # 各规模的船舶数（排序）

    low = [data[s]['risk_low_pct'] for s in sizes]  # 各规模低风险占比
    middle = [data[s]['risk_middle_pct'] for s in sizes]  # 各规模中风险占比
    high = [data[s]['risk_high_pct'] for s in sizes]  # 各规模高风险占比

    x = np.arange(len(sizes))  # X轴位置
    width = 0.5  # 柱状图宽度

    ax.bar(x, low, width, label='低风险', color='#2ECC71', edgecolor='white', linewidth=0.5)  # 低风险柱
    ax.bar(x, middle, width, bottom=low, label='中风险', color='#F39C12', edgecolor='white', linewidth=0.5)  # 中风险柱（叠加）
    ax.bar(x, high, width, bottom=np.array(low) + np.array(middle),  # 高风险柱（叠加）
           label='高风险', color='#E74C3C', edgecolor='white', linewidth=0.5)

    ax.set_xticks(x)  # 设置刻度位置
    ax.set_xticklabels([f'{s}艘' for s in sizes], fontsize=11)  # 设置刻度标签
    ax.set_title(f'{sys_color} - 风险等级分布', fontsize=13, fontweight='bold', pad=15)
    ax.set_ylabel('比例 (%)', fontsize=12)
    ax.legend(loc='upper left', fontsize=10)
    ax.set_ylim(0, 105)  # Y轴范围0-105%
    ax.grid(True, alpha=0.2, axis='y')  # Y轴网格

plt.tight_layout()
plt.savefig(OUTPUT_DIR / "chart02_scaling_risk_distribution.png")
plt.close()
print(f"  [OK] chart02_scaling_risk_distribution.png")


# ──────────────────────────────────────────────
# 5. 图表 3: 违规检测率 - 直接对比 (所有规模)
# ──────────────────────────────────────────────

print("\n[5/7] 生成 Agent vs Cascade 违规检测率对比图...")

fig, ax = plt.subplots(figsize=(10, 7))  # 创建单子图

# 收集所有可对比的规模
all_sizes = set()  # 所有存在的船舶数集合
for s in [scaling_data, capacity_data]:  # 从扩展性和容量数据中收集
    all_sizes.update(s['agent'].keys())  # Agent的规模
    all_sizes.update(s['cascade'].keys())  # Cascade的规模
all_sizes = sorted(all_sizes)  # 排序

agent_rates = []  # Agent的违规率列表
cascade_rates = []  # Cascade的违规率列表
labels = []  # 标签列表

for s in all_sizes:  # 遍历所有规模
    a_rate = None  # Agent违规率，初始化为空
    c_rate = None  # Cascade违规率，初始化为空
    # Try scaling first, then capacity
    if s in scaling_data['agent']:  # 优先从扩展性数据获取
        a_rate = scaling_data['agent'][s]['violation_rate'] * 100  # Agent违规率(%)
    elif s in capacity_data['agent']:  # 其次从容量数据获取
        a_rate = capacity_data['agent'][s]['violation_rate'] * 100  # Agent违规率(%)

    if s in scaling_data['cascade']:  # 优先从扩展性数据获取
        c_rate = scaling_data['cascade'][s]['violation_rate'] * 100  # Cascade违规率(%)
    elif s in capacity_data['cascade']:  # 其次从容量数据获取
        c_rate = capacity_data['cascade'][s]['violation_rate'] * 100  # Cascade违规率(%)

    if a_rate is not None and c_rate is not None:  # 两者都有数据才加入对比
        agent_rates.append(a_rate)
        cascade_rates.append(c_rate)
        labels.append(f'{s}艘')  # 标签格式

x = np.arange(len(labels))  # X轴位置
width = 0.35  # 每组柱状图宽度

bars1 = ax.bar(x - width/2, agent_rates, width, label='原始 Agent',  # Agent柱状图（左）
               color=C_AGENT, edgecolor='white', linewidth=0.5, alpha=0.9)
bars2 = ax.bar(x + width/2, cascade_rates, width, label='三级架构',  # Cascade柱状图（右）
               color=C_CASCADE, edgecolor='white', linewidth=0.5, alpha=0.9)

# 在柱子上标注数值
for bar in bars1:  # 遍历Agent各柱子
    h = bar.get_height()  # 柱子高度
    ax.text(bar.get_x() + bar.get_width()/2, h + 0.5, f'{h:.1f}%',
            ha='center', va='bottom', fontsize=9, color=C_AGENT, fontweight='bold')
for bar in bars2:  # 遍历Cascade各柱子
    h = bar.get_height()  # 柱子高度
    ax.text(bar.get_x() + bar.get_width()/2, h + 0.5, f'{h:.1f}%',
            ha='center', va='bottom', fontsize=9, color=C_CASCADE, fontweight='bold')

ax.set_ylabel('违规检测率 (%)', fontsize=13)
ax.set_xlabel('数据规模', fontsize=13)
ax.set_title('Agent vs Cascade 违规检测率对比', fontsize=15, fontweight='bold', pad=15)
ax.set_xticks(x)
ax.set_xticklabels(labels, fontsize=11)
ax.legend(fontsize=12, loc='upper left')
ax.grid(True, alpha=0.2, axis='y')
ax.set_ylim(0, max(max(agent_rates), max(cascade_rates)) * 1.3 + 5)  # Y轴留出标注空间

plt.tight_layout()
plt.savefig(OUTPUT_DIR / "chart03_violation_rate_comparison.png")
plt.close()
print(f"  [OK] chart03_violation_rate_comparison.png")


# ──────────────────────────────────────────────
# 6. 图表 4: Cascade 路由分布 (100艘)
# ──────────────────────────────────────────────

print("\n[6/7] 生成路由分布图...")

fig, axes = plt.subplots(1, 2, figsize=(14, 6))  # 创建1行2列子图

# 数据: scaling cascade 100 (如果有)
cascade_100 = scaling_data['cascade'].get(100, speed_data.get('cascade'))  # 获取100艘时的Cascade数据
if cascade_100 and cascade_100.get('routings'):  # 如果有路由数据
    routing_counts = cascade_100['routing_counts']  # 路由分配计数
    labels_route = list(routing_counts.keys())  # 路由类别标签
    values = list(routing_counts.values())  # 各类别数量
    colors_route = [ROUTING_COLORS.get(k, '#95A5A6') for k in labels_route]  # 对应颜色列表

    # 饼图
    ax = axes[0]  # 左侧子图
    wedges, texts, autotexts = ax.pie(  # 绘制饼图
        values, labels=labels_route, autopct='%1.1f%%',
        colors=colors_route, startangle=90,
        textprops={'fontsize': 11},
        wedgeprops={'edgecolor': 'white', 'linewidth': 1.5}
    )
    for at in autotexts:  # 设置百分比文本样式
        at.set_fontweight('bold')
        at.set_fontsize(11)
    ax.set_title('三级架构路由分布 (100艘)', fontsize=13, fontweight='bold', pad=15)

    # 水平柱状图
    ax = axes[1]  # 右侧子图
    y_pos = np.arange(len(labels_route))  # Y轴位置
    bars = ax.barh(y_pos, values, color=colors_route, edgecolor='white', linewidth=0.5, height=0.6)  # 水平柱状图
    for bar, v in zip(bars, values):  # 在柱子右侧标注数值
        ax.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height()/2,
                f'{v} ({v/sum(values)*100:.1f}%)',
                va='center', fontsize=11, fontweight='bold')

    ax.set_yticks(y_pos)  # 设置Y轴刻度
    ax.set_yticklabels(labels_route, fontsize=11)  # 设置Y轴标签
    ax.set_xlabel('船舶数量', fontsize=12)
    ax.set_title('路由分布详情', fontsize=13, fontweight='bold', pad=15)
    ax.invert_yaxis()  # Y轴反转（顶部显示最大值）
    ax.grid(True, alpha=0.2, axis='x')  # X轴网格

plt.tight_layout()
plt.savefig(OUTPUT_DIR / "chart04_routing_distribution.png")
plt.close()
print(f"  [OK] chart04_routing_distribution.png")


# ──────────────────────────────────────────────
# 7. 图表 5: 容量测试 - 存活 & 风险对比
# ──────────────────────────────────────────────

print("\n[7/7] 生成容量测试对比图...")

fig, axes = plt.subplots(1, 2, figsize=(16, 6))  # 创建1行2列子图

# 左: 违规率随规模变化 (对比 Agent vs Cascade)
ax = axes[0]  # 左侧子图
all_cap_sizes = sorted(set(capacity_data['agent'].keys()) | set(capacity_data['cascade'].keys()))  # 所有容量测试规模的并集（排序）

agent_rates_cap = []  # Agent违规率列表（容量测试）
cascade_rates_cap = []  # Cascade违规率列表（容量测试）
cap_labels = []  # 容量测试标签列表

for s in all_cap_sizes:  # 遍历所有规模
    if s in capacity_data['agent'] and s in capacity_data['cascade']:  # 两者都有数据
        agent_rates_cap.append(capacity_data['agent'][s]['violation_rate'] * 100)  # Agent违规率
        cascade_rates_cap.append(capacity_data['cascade'][s]['violation_rate'] * 100)  # Cascade违规率
        cap_labels.append(f'{s}艘')  # 标签

x = np.arange(len(cap_labels))  # X轴位置
width = 0.35  # 柱宽

ax.bar(x - width/2, agent_rates_cap, width, label='原始 Agent',  # Agent柱状图（左）
       color=C_AGENT, edgecolor='white', linewidth=0.5, alpha=0.9)
ax.bar(x + width/2, cascade_rates_cap, width, label='三级架构',  # Cascade柱状图（右）
       color=C_CASCADE, edgecolor='white', linewidth=0.5, alpha=0.9)

for i in range(len(cap_labels)):  # 在每个柱子上标注数值
    ax.text(x[i] - width/2, agent_rates_cap[i] + 0.8, f'{agent_rates_cap[i]:.1f}%',
            ha='center', va='bottom', fontsize=9, color=C_AGENT, fontweight='bold')
    ax.text(x[i] + width/2, cascade_rates_cap[i] + 0.8, f'{cascade_rates_cap[i]:.1f}%',
            ha='center', va='bottom', fontsize=9, color=C_CASCADE, fontweight='bold')

ax.set_xticks(x)
ax.set_xticklabels(cap_labels, fontsize=11)
ax.set_xlabel('数据规模 (船舶数)', fontsize=12)
ax.set_ylabel('违规检测率 (%)', fontsize=12)
ax.set_title('容量压力测试 - 违规检测率对比', fontsize=13, fontweight='bold', pad=15)
ax.legend(fontsize=11)
ax.grid(True, alpha=0.2, axis='y')

# 右: 平均置信度对比
ax = axes[1]  # 右侧子图
agent_conf = []  # Agent平均置信度列表
cascade_conf = []  # Cascade平均置信度列表

for s in all_cap_sizes:  # 遍历所有规模
    if s in capacity_data['agent'] and s in capacity_data['cascade']:  # 两种系统都有此规模的数据
        agent_conf.append(capacity_data['agent'][s]['confidence_mean'])  # Agent平均置信度
        cascade_conf.append(capacity_data['cascade'][s]['confidence_mean'])  # Cascade平均置信度

x = np.arange(len(cap_labels))  # X轴位置
ax.plot(x, agent_conf, 'o-', color=C_AGENT, linewidth=2.5, markersize=8, label='原始 Agent')  # Agent折线图
ax.plot(x, cascade_conf, 's-', color=C_CASCADE, linewidth=2.5, markersize=8, label='三级架构')  # Cascade折线图

for i in range(len(cap_labels)):  # 在每个数据点上标注数值
    ax.text(x[i], agent_conf[i] + 0.01, f'{agent_conf[i]:.3f}',
            ha='center', fontsize=9, color=C_AGENT, fontweight='bold')
    ax.text(x[i], cascade_conf[i] - 0.03, f'{cascade_conf[i]:.3f}',
            ha='center', fontsize=9, color=C_CASCADE, fontweight='bold')

ax.set_xticks(x)
ax.set_xticklabels(cap_labels, fontsize=11)
ax.set_xlabel('数据规模 (船舶数)', fontsize=12)
ax.set_ylabel('平均置信度', fontsize=12)
ax.set_title('容量压力测试 - 平均置信度对比', fontsize=13, fontweight='bold', pad=15)
ax.legend(fontsize=11)
ax.grid(True, alpha=0.2)
ax.set_ylim(min(min(agent_conf), min(cascade_conf)) - 0.05,  # Y轴范围（留出边距）
            max(max(agent_conf), max(cascade_conf)) + 0.05)

plt.tight_layout()
plt.savefig(OUTPUT_DIR / "chart05_capacity_comparison.png")
plt.close()
print(f"  [OK] chart05_capacity_comparison.png")


# ──────────────────────────────────────────────
# 额外图表 6: 综合对比仪表板
# ──────────────────────────────────────────────

print("\n[额外] 生成综合对比仪表板...")

fig, axes = plt.subplots(2, 2, figsize=(16, 12))  # 创建2行2列子图

# (1,1) - 100艘时风险分布对比
ax = axes[0, 0]  # 左上子图
agent_100 = speed_data.get('agent', scaling_data['agent'].get(100))  # 获取Agent在100艘时的数据
cascade_100 = speed_data.get('cascade', scaling_data['cascade'].get(100))  # 获取Cascade在100艘时的数据

if agent_100 and cascade_100:  # 如果两种数据都存在
    categories = ['低风险', '中风险', '高风险']  # 风险等级类别
    agent_vals = [agent_100['risk_low_pct'], agent_100['risk_middle_pct'], agent_100['risk_high_pct']]  # Agent各风险占比
    cascade_vals = [cascade_100['risk_low_pct'], cascade_100['risk_middle_pct'], cascade_100['risk_high_pct']]  # Cascade各风险占比

    x = np.arange(len(categories))  # X轴位置
    width = 0.3  # 柱宽

    ax.bar(x - width/2, agent_vals, width, label='Agent',  # Agent柱状图（左）
           color=C_AGENT, edgecolor='white', linewidth=0.5, alpha=0.85)
    ax.bar(x + width/2, cascade_vals, width, label='Cascade',  # Cascade柱状图（右）
           color=C_CASCADE, edgecolor='white', linewidth=0.5, alpha=0.85)

    for i in range(len(categories)):  # 在每个柱子上标注数值
        ax.text(x[i] - width/2, agent_vals[i] + 1, f'{agent_vals[i]:.1f}%',
                ha='center', fontsize=9, fontweight='bold', color=C_AGENT)
        ax.text(x[i] + width/2, cascade_vals[i] + 1, f'{cascade_vals[i]:.1f}%',
                ha='center', fontsize=9, fontweight='bold', color=C_CASCADE)

    ax.set_xticks(x)
    ax.set_xticklabels(categories, fontsize=11)
    ax.set_ylabel('比例 (%)', fontsize=11)
    ax.set_title('风险等级分布 (100艘)', fontsize=13, fontweight='bold', pad=10)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.2, axis='y')

# (1,2) - 置信度分布箱线图（用误差条形图表示）
ax = axes[0, 1]  # 右上子图
if agent_100 and cascade_100:
    agent_ci = agent_100['confidence_mean']  # Agent平均置信度
    agent_ci_std = agent_100['confidence_std']  # Agent置信度标准差
    cascade_ci = cascade_100['confidence_mean']  # Cascade平均置信度
    cascade_ci_std = cascade_100['confidence_std']  # Cascade置信度标准差

    # 从 scaling 数据收集各规模的置信度
    scale_sizes = sorted(scaling_data['agent'].keys()) if scaling_data['agent'] else []  # 各规模船舶数
    agent_means = [scaling_data['agent'][s]['confidence_mean'] for s in scale_sizes]  # Agent各规模平均置信度
    agent_stds = [scaling_data['agent'][s]['confidence_std'] for s in scale_sizes]  # Agent各规模置信度标准差
    cascade_means = [scaling_data['cascade'][s]['confidence_mean'] for s in scale_sizes]  # Cascade各规模平均置信度
    cascade_stds = [scaling_data['cascade'][s]['confidence_std'] for s in scale_sizes]  # Cascade各规模置信度标准差

    x = np.arange(len(scale_sizes))  # X轴位置
    width = 0.35  # 柱宽

    ax.bar(x - width/2, agent_means, width, yerr=agent_stds,  # Agent带误差棒的柱状图
           label='Agent', color=C_AGENT, edgecolor='white', linewidth=0.5,
           alpha=0.85, capsize=4, error_kw={'linewidth': 1.5})
    ax.bar(x + width/2, cascade_means, width, yerr=cascade_stds,  # Cascade带误差棒的柱状图
           label='Cascade', color=C_CASCADE, edgecolor='white', linewidth=0.5,
           alpha=0.85, capsize=4, error_kw={'linewidth': 1.5})

    ax.set_xticks(x)
    ax.set_xticklabels([f'{s}艘' for s in scale_sizes], fontsize=11)
    ax.set_ylabel('平均置信度', fontsize=11)
    ax.set_title('置信度对比 (均值±标准差)', fontsize=13, fontweight='bold', pad=10)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.2, axis='y')
    ax.set_ylim(0.4, 1.0)  # Y轴范围

# (2,1) - 各规模综合违规率趋势
ax = axes[1, 0]  # 左下子图
all_sizes = sorted(set(list(scaling_data['agent'].keys()) + list(capacity_data['agent'].keys())))  # 所有可对比的船舶规模集合（排序）
agent_all = []  # Agent各规模违规率列表
cascade_all = []  # Cascade各规模违规率列表
sizes_all = []  # 有完整对比数据的规模列表
for s in all_sizes:  # 遍历所有船舶规模
    a_r = scaling_data['agent'].get(s, capacity_data['agent'].get(s))  # 获取Agent在该规模下的数据
    c_r = scaling_data['cascade'].get(s, capacity_data['cascade'].get(s))  # 获取Cascade在该规模下的数据
    if a_r and c_r:  # 如果两种系统都有数据
        sizes_all.append(s)  # 记录此规模
        agent_all.append(a_r['violation_rate'] * 100)  # Agent违规率(%)
        cascade_all.append(c_r['violation_rate'] * 100)  # Cascade违规率(%)

ax.plot(sizes_all, agent_all, 'o-', color=C_AGENT, linewidth=2.5, markersize=8, label='原始 Agent')
ax.plot(sizes_all, cascade_all, 's-', color=C_CASCADE, linewidth=2.5, markersize=8, label='三级架构')
ax.fill_between(sizes_all, agent_all, cascade_all, alpha=0.1,
                color=C_CASCADE, label='差异区域')

for s, a, c in zip(sizes_all, agent_all, cascade_all):
    ax.annotate(f'A:{a:.1f}%', (s, a), textcoords="offset points",
                xytext=(0, -18), ha='center', fontsize=8, color=C_AGENT)
    ax.annotate(f'C:{c:.1f}%', (s, c), textcoords="offset points",
                xytext=(0, 10), ha='center', fontsize=8, color=C_CASCADE)

ax.set_xlabel('船舶数量', fontsize=11)
ax.set_ylabel('违规检测率 (%)', fontsize=11)
ax.set_title('违规检测率趋势 (全规模)', fontsize=13, fontweight='bold', pad=10)
ax.legend(fontsize=10)
ax.grid(True, alpha=0.3)
ax.set_xlim(0, max(sizes_all) * 1.1)

# (2,2) - 容错实验
ax = axes[1, 1]  # 右下子图
fault_names = []  # 故障类型名称列表
agent_success = []  # Agent各故障类型成功率列表
cascade_success = []  # Cascade各故障类型成功率列表
for fault_type in ['fault_nollm', 'fault_nomodel']:  # 遍历两种故障类型
    fd = fault_data[fault_type]  # 当前故障类型的实验数据
    if fd['agent'] and fd['cascade']:  # 如果两种系统都有数据
        a_trials = len(fd['agent'])  # Agent试验次数
        c_trials = len(fd['cascade'])  # Cascade试验次数
        a_success = sum(1 for m in fd['agent'] if m)  # Agent成功次数（有数据即成功）
        c_success = sum(1 for m in fd['cascade'] if m)  # Cascade成功次数
        label = 'LLM不可用' if 'nollm' in fault_type else 'ML模型缺失'  # 故障类型中文标签
        fault_names.append(label)  # 添加到故障类型名称列表
        agent_success.append(a_success / a_trials * 100)  # Agent成功率(%)
        cascade_success.append(c_success / c_trials * 100)  # Cascade成功率(%)

if fault_names:
    x = np.arange(len(fault_names))
    width = 0.35
    ax.bar(x - width/2, agent_success, width, label='Agent',
           color=C_AGENT, edgecolor='white', linewidth=0.5, alpha=0.85)
    ax.bar(x + width/2, cascade_success, width, label='Cascade',
           color=C_CASCADE, edgecolor='white', linewidth=0.5, alpha=0.85)

    for i in range(len(fault_names)):  # 在每组柱子上标注数值
        ax.text(x[i] - width/2, agent_success[i] + 1, f'{agent_success[i]:.0f}%',
                ha='center', fontsize=10, fontweight='bold', color=C_AGENT)
        ax.text(x[i] + width/2, cascade_success[i] + 1, f'{cascade_success[i]:.0f}%',
                ha='center', fontsize=10, fontweight='bold', color=C_CASCADE)

    ax.set_xticks(x)
    ax.set_xticklabels(fault_names, fontsize=11)
    ax.set_ylabel('成功率 (%)', fontsize=11)
    ax.set_title('容错实验 - 系统存活率', fontsize=13, fontweight='bold', pad=10)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.2, axis='y')
    ax.set_ylim(0, 110)
else:
    ax.text(0.5, 0.5, '容错数据不可用', ha='center', va='center',
            transform=ax.transAxes, fontsize=14, color='gray')

plt.tight_layout()
plt.savefig(OUTPUT_DIR / "chart06_overview_dashboard.png")
plt.close()
print(f"  [OK] chart06_overview_dashboard.png")

# ── 生成故障横向对比图 (如果数据够) ──
if len(fault_data) >= 1:
    print("\n[额外] 生成容错对比图...")
    fig, ax = plt.subplots(figsize=(10, 6))

    fault_types = []  # 故障类型标签列表
    a_success_rates = []  # Agent各故障类型成功率
    c_success_rates = []  # Cascade各故障类型成功率
    a_violation_rates = []  # Agent各故障类型违规率
    c_violation_rates = []  # Cascade各故障类型违规率

    for fault_type in ['fault_nollm', 'fault_nomodel']:  # 遍历两种故障类型
        fd = fault_data[fault_type]  # 当前故障类型的实验数据
        if fd['agent'] and fd['cascade']:  # 如果两种系统都有数据
            label = 'LLM不可用' if 'nollm' in fault_type else 'ML模型缺失'  # 故障类型的中文标签
            fault_types.append(label)  # 添加到标签列表
            # 平均违规检测率
            a_v = np.mean([m['violation_rate'] for m in fd['agent'] if m]) * 100  # Agent平均违规检测率(%)
            c_v = np.mean([m['violation_rate'] for m in fd['cascade'] if m]) * 100  # Cascade平均违规检测率(%)
            a_violation_rates.append(a_v)  # 添加到Agent违规率列表
            c_violation_rates.append(c_v)  # 添加到Cascade违规率列表

    if fault_types:
        x = np.arange(len(fault_types))
        width = 0.35

        ax.bar(x - width/2, a_violation_rates, width, label='Agent',
               color=C_AGENT, edgecolor='white', linewidth=0.5, alpha=0.85)
        ax.bar(x + width/2, c_violation_rates, width, label='Cascade',
               color=C_CASCADE, edgecolor='white', linewidth=0.5, alpha=0.85)

        for i in range(len(fault_types)):  # 在每组柱子上标注数值
            ax.text(x[i] - width/2, a_violation_rates[i] + 0.5, f'{a_violation_rates[i]:.1f}%',
                    ha='center', fontsize=11, fontweight='bold', color=C_AGENT)
            ax.text(x[i] + width/2, c_violation_rates[i] + 0.5, f'{c_violation_rates[i]:.1f}%',
                    ha='center', fontsize=11, fontweight='bold', color=C_CASCADE)

        ax.set_xticks(x)
        ax.set_xticklabels(fault_types, fontsize=12)
        ax.set_ylabel('违规检测率 (%)', fontsize=12)
        ax.set_title('容错环境下的违规检测率对比', fontsize=14, fontweight='bold', pad=15)
        ax.legend(fontsize=11)
        ax.grid(True, alpha=0.2, axis='y')

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / "chart07_fault_tolerance.png")
    plt.close()
    print(f"  [OK] chart07_fault_tolerance.png")

# ── 打印摘要 ──
print("\n" + "=" * 60)
print("  生成完成!")
print("=" * 60)
print(f"\n  输出目录: {OUTPUT_DIR}/")
print(f"\n  📄 CSV 文件:")
print(f"    - experiment_summary.csv  (汇总数据)")
print(f"\n  🖼️  图表文件:")
print(f"    - chart01_scaling_violation_rate.png  (扩展性违规率)")
print(f"    - chart02_scaling_risk_distribution.png  (风险分布)")
print(f"    - chart03_violation_rate_comparison.png  (违规率对比)")
print(f"    - chart04_routing_distribution.png  (路由分布)")
print(f"    - chart05_capacity_comparison.png  (容量测试)")
print(f"    - chart06_overview_dashboard.png  (综合仪表板)")
print(f"    - chart07_fault_tolerance.png  (容错对比)")

# 列出存在问题的故障数据
for fault_type in ['fault_nollm', 'fault_nomodel']:  # 遍历故障类型
    fd = fault_data[fault_type]  # 当前故障类型的数据
    if not fd['agent'] and not fd['cascade']:  # 如果两种系统都没有数据
        print(f"\n  ⚠  缺失: {fault_type}")
