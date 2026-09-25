"""
图表生成器 —— 生成实验对比可视化（PNG 图表）
=============================================
"""
from __future__ import annotations

import math
import numpy as np
from pathlib import Path

# ── 全局 matplotlib 配置 ──
_MPL_IMPORTED = False  # matplotlib是否已导入标志
def _ensure_mpl():
    global _MPL_IMPORTED
    if _MPL_IMPORTED:
        return
    import matplotlib
    matplotlib.use('Agg')  # 使用非交互式后端（无GUI）
    import matplotlib.pyplot as plt
    plt.rcParams.update({  # 更新全局matplotlib配置
        'font.family': 'sans-serif',  # 无衬线字体
        'font.sans-serif': ['Microsoft YaHei', 'SimHei', 'DejaVu Sans'],  # 中英文字体列表
        'font.size': 12,  # 默认字体大小
        'axes.unicode_minus': False,  # 正确显示负号
        'figure.dpi': 150,  # 图形分辨率
        'savefig.dpi': 300,  # 保存图片分辨率
        'savefig.bbox': 'tight',  # 保存时紧凑裁剪
    })
    _MPL_IMPORTED = True  # 标记为已导入

# 调色板
C_AGENT = '#E74C3C'  # 原始Agent的颜色（红色）
C_CASCADE = '#2980B9'  # 三级架构的颜色（蓝色）
C_AGENT_LIGHT = '#F1948A'  # 原始Agent的浅色变体
C_CASCADE_LIGHT = '#85C1E9'  # 三级架构的浅色变体

RISK_COLORS = {'low': '#2ECC71', 'middle': '#F39C12', 'high': '#E74C3C'}  # 风险等级对应的颜色
ROUTING_LABELS = {  # 路由标签映射（代码名→显示名）
    'normal': '正常 (normal)',
    'needs_review': '需审查 (needs_review)',
    'obvious_issue': '明显问题 (obvious_issue)',
    'has_risk': '有风险 (has_risk)',
}
ROUTING_COLORS = {  # 各路由类别对应的颜色
    'normal': '#2ECC71',  # 正常→绿色
    'needs_review': '#F39C12',  # 需审查→橙色
    'obvious_issue': '#E74C3C',  # 明显问题→红色
    'has_risk': '#9B59B6',  # 有风险→紫色
}


class ChartGenerator:
    """生成实验对比图表（PNG）"""

    def generate_all(
        self,
        speed_results: dict,  # 速度测试结果字典
        capacity_results: dict,  # 容量测试结果字典
        output_dir: str,  # 输出目录路径
    ) -> list[str]:
        """生成全部图表"""
        _ensure_mpl()  # 确保matplotlib已导入配置
        import matplotlib.pyplot as plt

        out = Path(output_dir)  # 输出目录的Path对象
        out.mkdir(parents=True, exist_ok=True)  # 递归创建输出目录

        charts = []  # 已生成的图表文件路径列表

        # 1. LLM 调用次数对比
        if "detail" in speed_results.get("llm_calls", {}):
            path = str(out / "chart_llm_calls.png")  # LLM对比图表输出路径
            self._chart_llm_calls(speed_results["llm_calls"]["detail"], path)
            charts.append(path)

        # 2. 墙钟时间对比
        if "wallclock" in speed_results:
            path = str(out / "chart_wallclock.png")  # 时间对比图表输出路径
            self._chart_wallclock(speed_results["wallclock"], path)
            charts.append(path)

        # 3. 可扩展性曲线
        if "scaling" in speed_results:
            path = str(out / "chart_scaling.png")  # 可扩展性图表输出路径
            self._chart_scaling(speed_results["scaling"], path)
            charts.append(path)

        # 4. 容量存活
        if "capacity" in capacity_results:
            path = str(out / "chart_capacity.png")  # 容量测试图表输出路径
            self._chart_capacity(capacity_results["capacity"], path)
            charts.append(path)

        # 5. 综合仪表板（如果数据充分）
        if "detail" in speed_results.get("llm_calls", {}) \
           and "wallclock" in speed_results:
            path = str(out / "chart_overview.png")  # 综合仪表板图表输出路径
            self._chart_overview(speed_results, capacity_results, path)
            charts.append(path)

        plt.close('all')  # 关闭所有matplotlib图形
        return charts

    # ──────────────────────────────────────────────
    # 图表 1: LLM 调用次数对比
    # ──────────────────────────────────────────────

    def _chart_llm_calls(self, detail: dict, path: str):  # detail: LLM调用详情字典; path: 图片保存路径
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8, 6))  # 创建8x6英寸的图形和坐标轴

        agent = detail["agent"]  # 原始Agent的LLM统计数据
        cascade = detail["cascade"]  # 三级架构的LLM统计数据

        categories = ['总调用次数', '每船均值', '每船中位数']  # 三个对比类别
        a_vals = [agent['total'], agent['mean'], agent['median']]  # Agent的统计数据值
        c_vals = [cascade['total'], cascade['mean'], cascade['median']]  # Cascade的统计数据值

        x = np.arange(len(categories))  # X轴位置数组
        width = 0.3  # 柱状图宽度

        bars1 = ax.bar(x - width/2, a_vals, width, label='原始 Agent',  # Agent柱状图
                       color=C_AGENT, edgecolor='white', linewidth=0.5, alpha=0.9)
        bars2 = ax.bar(x + width/2, c_vals, width, label='三级架构',  # Cascade柱状图
                       color=C_CASCADE, edgecolor='white', linewidth=0.5, alpha=0.9)

        for bar in bars1:  # 遍历Agent柱状条
            h = bar.get_height()  # 柱状条高度
            ax.text(bar.get_x() + bar.get_width()/2, h + max(a_vals)*0.02,  # 在柱状条上方标注数值
                    f'{h:.1f}', ha='center', va='bottom', fontsize=10,
                    color=C_AGENT, fontweight='bold')
        for bar in bars2:  # 遍历Cascade柱状条
            h = bar.get_height()
            ax.text(bar.get_x() + bar.get_width()/2, h + max(a_vals)*0.02,
                    f'{h:.1f}', ha='center', va='bottom', fontsize=10,
                    color=C_CASCADE, fontweight='bold')

        # 标注减少百分比
        reduction = detail.get("comparison", {}).get("total_reduction", "N/A")  # 总减少百分比
        ax.text(0.5, 0.92, f'LLM 调用减少: {reduction}',  # 在图表上方显示减少信息
                transform=ax.transAxes, ha='center', fontsize=13,
                fontweight='bold', color='#27AE60',
                bbox=dict(boxstyle='round,pad=0.3', facecolor='#D5F5E3', alpha=0.8))

        ax.set_xticks(x)  # 设置X轴刻度位置
        ax.set_xticklabels(categories, fontsize=11)  # 设置X轴刻度标签
        ax.set_ylabel('次数', fontsize=12)  # Y轴标签
        ax.set_title('LLM 调用次数对比', fontsize=14, fontweight='bold', pad=15)  # 图表标题
        ax.legend(fontsize=11, loc='upper right')  # 图例
        ax.grid(True, alpha=0.2, axis='y')  # 网格线

        # 置信区间标注
        ci_text = (f"Agent 95%CI: [{agent['ci_95'][0]:.2f}, {agent['ci_95'][1]:.2f}]\n"  # Agent的95%置信区间
                   f"Cascade 95%CI: [{cascade['ci_95'][0]:.2f}, {cascade['ci_95'][1]:.2f}]")  # Cascade的95%置信区间
        ax.text(0.98, 0.05, ci_text, transform=ax.transAxes, fontsize=9,  # 在右下角显示置信区间
                verticalalignment='bottom', horizontalalignment='right',
                bbox=dict(boxstyle='round', facecolor='#F0F0F0', alpha=0.8))

        plt.tight_layout()  # 自动调整布局
        plt.savefig(path)  # 保存图表到文件
        plt.close(fig)  # 关闭图形释放内存

    # ──────────────────────────────────────────────
    # 图表 2: 墙钟时间对比
    # ──────────────────────────────────────────────

    def _chart_wallclock(self, wallclock: dict, path: str):  # wallclock: 时间对比数据字典; path: 图片保存路径
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8, 5))  # 创建8x5英寸的图形和坐标轴

        categories = ['总时间 (秒)', '每船时间 (秒)']  # 两个对比类别
        a_vals = [wallclock['agent_total_seconds'], wallclock['agent_per_ship_seconds']]  # Agent的时间值
        c_vals = [wallclock['cascade_total_seconds'], wallclock['cascade_per_ship_seconds']]  # Cascade的时间值

        x = np.arange(len(categories))  # X轴位置数组
        width = 0.35  # 柱状图宽度

        bars1 = ax.bar(x - width/2, a_vals, width, label='原始 Agent',  # Agent柱状图
                       color=C_AGENT, edgecolor='white', linewidth=0.5, alpha=0.9)
        bars2 = ax.bar(x + width/2, c_vals, width, label='三级架构',  # Cascade柱状图
                       color=C_CASCADE, edgecolor='white', linewidth=0.5, alpha=0.9)

        for bar in bars1:  # 遍历Agent柱状条
            h = bar.get_height()  # 柱状条高度
            ax.text(bar.get_x() + bar.get_width()/2, h + max(a_vals)*0.02,
                    f'{h:.1f}s', ha='center', va='bottom', fontsize=10,
                    fontweight='bold', color=C_AGENT)
        for bar in bars2:  # 遍历Cascade柱状条
            h = bar.get_height()
            ax.text(bar.get_x() + bar.get_width()/2, h + max(a_vals)*0.02,
                    f'{h:.1f}s', ha='center', va='bottom', fontsize=10,
                    fontweight='bold', color=C_CASCADE)

        speedup = wallclock.get('speedup_ratio', 0)  # 加速比
        ax.text(0.5, 0.92, f'加速比: {speedup:.1f}x',  # 在图表上方显示加速比
                transform=ax.transAxes, ha='center', fontsize=14,
                fontweight='bold', color='#27AE60',
                bbox=dict(boxstyle='round,pad=0.3', facecolor='#D5F5E3', alpha=0.8))

        ax.set_xticks(x)  # 设置X轴刻度位置
        ax.set_xticklabels(categories, fontsize=11)  # 设置X轴刻度标签
        ax.set_ylabel('时间', fontsize=12)  # Y轴标签
        ax.set_title('处理时间对比', fontsize=14, fontweight='bold', pad=15)  # 图表标题
        ax.legend(fontsize=11)  # 图例
        ax.grid(True, alpha=0.2, axis='y')  # 网格线

        plt.tight_layout()
        plt.savefig(path)
        plt.close(fig)

    # ──────────────────────────────────────────────
    # 图表 3: 可扩展性曲线 (log-log)
    # ──────────────────────────────────────────────

    def _chart_scaling(self, scaling: dict, path: str):  # scaling: 可扩展性分析数据字典; path: 图片保存路径
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8, 6))  # 创建8x6英寸的图形和坐标轴

        # 从当前结果中恢复 N 和 T 的近似值（实际应该在 speed_result 里存 raw data）
        # 如果 run.py 传入了原始数据，这里用 scaling 信息显示回归结果
        agent = scaling.get("agent", {})  # Agent的可扩展性数据
        cascade = scaling.get("cascade", {})  # Cascade的可扩展性数据

        info_lines = []  # 显示信息行列表
        if agent:
            info_lines.append(f"Agent:  α={agent.get('alpha', '?'):.2f}"  # Agent的缩放指数α
                              f"  R²={agent.get('r_squared', '?'):.3f}")  # Agent的拟合优度R²
            info_lines.append(f"       {agent.get('interpretation', '')}")  # Agent的扩展性解释
        if cascade:
            info_lines.append(f"Cascade: α={cascade.get('alpha', '?'):.2f}"  # Cascade的缩放指数α
                              f"  R²={cascade.get('r_squared', '?'):.3f}")  # Cascade的拟合优度R²
            info_lines.append(f"        {cascade.get('interpretation', '')}")  # Cascade的扩展性解释

        ax.text(0.5, 0.5, '\n'.join(info_lines),  # 在图表中央显示信息
                transform=ax.transAxes, ha='center', va='center',
                fontsize=13, fontweight='bold',
                bbox=dict(boxstyle='round,pad=1', facecolor='#F0F0F0', alpha=0.9))

        ax.set_xlabel('log(船舶数)', fontsize=12)  # X轴标签
        ax.set_ylabel('log(处理时间)', fontsize=12)  # Y轴标签
        ax.set_title('可扩展性分析 (log-log 回归)', fontsize=14, fontweight='bold', pad=15)
        ax.grid(True, alpha=0.3)  # 网格线

        # 画 α=1 参考线示意
        xlim = ax.get_xlim()  # 当前X轴范围
        ylim = ax.get_ylim()  # 当前Y轴范围

        plt.tight_layout()
        plt.savefig(path)
        plt.close(fig)

    # ──────────────────────────────────────────────
    # 图表 4: 容量存活对比
    # ──────────────────────────────────────────────

    def _chart_capacity(self, capacity: dict, path: str):  # capacity: 容量测试数据字典; path: 图片保存路径
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(1, 2, figsize=(14, 6))  # 创建1行2列的子图布局，14x6英寸

        # 左图: 存活/死亡阶梯图
        ax = axes[0]  # 左侧子图
        detail = capacity.get("survival_detail", {})  # 各规模下的存活详情
        sizes_sorted = sorted(detail.items(), key=lambda x: int(x[0]))  # 按船舶数量排序

        ship_counts = [int(k) for k, v in sizes_sorted]  # 各测试规模的船舶数量列表
        agent_alive = [1 if v['agent'] == '存活' else 0 for _, v in sizes_sorted]  # Agent存活标志（1存活/0死亡）
        cascade_alive = [1 if v['cascade'] == '存活' else 0 for _, v in sizes_sorted]  # Cascade存活标志

        x = np.arange(len(ship_counts))  # X轴位置数组
        width = 0.3  # 柱状图宽度

        ax.bar(x - width/2, agent_alive, width, label='Agent',  # Agent存活柱状图
               color=C_AGENT, edgecolor='white', linewidth=0.5, alpha=0.85)
        ax.bar(x + width/2, cascade_alive, width, label='Cascade',  # Cascade存活柱状图
               color=C_CASCADE, edgecolor='white', linewidth=0.5, alpha=0.85)

        for i in range(len(ship_counts)):  # 遍历所有测试规模
            a_text = '存活' if agent_alive[i] else '死亡'  # Agent状态文本
            c_text = '存活' if cascade_alive[i] else '死亡'  # Cascade状态文本
            ax.text(x[i] - width/2, agent_alive[i] + 0.03, a_text,  # 标注Agent状态
                    ha='center', fontsize=10, fontweight='bold',
                    color=C_AGENT)
            ax.text(x[i] + width/2, cascade_alive[i] + 0.03, c_text,  # 标注Cascade状态
                    ha='center', fontsize=10, fontweight='bold',
                    color=C_CASCADE)

        ax.set_xticks(x)  # 设置X轴刻度位置
        ax.set_xticklabels([f'{s}艘' for s in ship_counts], fontsize=11)  # 设置X轴刻度标签
        ax.set_ylabel('状态', fontsize=12)  # Y轴标签
        ax.set_title('存活状态', fontsize=13, fontweight='bold', pad=10)  # 子图标题
        ax.set_yticks([0, 1])  # Y轴刻度（仅0和1）
        ax.set_yticklabels(['死亡', '存活'])  # Y轴刻度标签
        ax.legend(fontsize=11)  # 图例
        ax.set_ylim(-0.1, 1.3)  # Y轴范围
        ax.grid(True, alpha=0.2, axis='y')  # 网格线

        # 右图: 处理时间变化
        ax = axes[1]  # 右侧子图
        a_times = [v.get('agent_time_s', 0) for _, v in sizes_sorted]  # Agent各规模处理时间列表
        c_times = [v.get('cascade_time_s', 0) for _, v in sizes_sorted]  # Cascade各规模处理时间列表

        ax.plot(ship_counts, a_times, 'o-', color=C_AGENT, linewidth=2.5,  # Agent时间曲线（圆点实线）
                markersize=8, label='Agent')
        ax.plot(ship_counts, c_times, 's-', color=C_CASCADE, linewidth=2.5,  # Cascade时间曲线（方块实线）
                markersize=8, label='Cascade')

        for s, a, c_ in zip(ship_counts, a_times, c_times):  # 遍历各规模的数据点
            ax.annotate(f'{a:.0f}s', (s, a), textcoords="offset points",  # 标注Agent时间
                        xytext=(0, -15), ha='center', fontsize=9, color=C_AGENT)
            ax.annotate(f'{c_:.0f}s', (s, c_), textcoords="offset points",  # 标注Cascade时间
                        xytext=(0, 10), ha='center', fontsize=9, color=C_CASCADE)

        ax.set_xlabel('船舶数量', fontsize=12)  # X轴标签
        ax.set_ylabel('处理时间 (秒)', fontsize=12)  # Y轴标签
        ax.set_title('处理时间随规模变化', fontsize=13, fontweight='bold', pad=10)  # 子图标题
        ax.legend(fontsize=11)  # 图例
        ax.grid(True, alpha=0.3)  # 网格线

        # 摘要信息
        ratio = capacity.get('capacity_ratio', 0)  # 容量倍数（Cascade最大/Agent最大）
        fig.suptitle(f'容量压力测试  |  Agent 最大: {capacity.get("agent_max_capacity", 0)}艘'  # 总标题
                     f'  Cascade 最大: {capacity.get("cascade_max_capacity", 0)}艘'
                     f'  容量倍数: {ratio:.0f}x',
                     fontsize=13, fontweight='bold', y=1.02)

        plt.tight_layout()
        plt.savefig(path)
        plt.close(fig)

    # ──────────────────────────────────────────────
    # 图表 5: 综合仪表板
    # ──────────────────────────────────────────────

    def _chart_overview(self, speed_results: dict, capacity_results: dict, path: str):  # speed_results: 速度测试结果; capacity_results: 容量测试结果; path: 图片保存路径
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(2, 2, figsize=(16, 12))  # 创建2行2列的子图布局，16x12英寸

        llm = speed_results.get("llm_calls", {})  # LLM调用数据
        wall = speed_results.get("wallclock", {})  # 墙钟时间数据

        # (1,1) LLM 调用
        ax = axes[0, 0]  # 左上子图
        if "detail" in llm:
            d = llm["detail"]  # LLM调用详情
            a_total = d['agent']['total']  # Agent总LLM调用次数
            c_total = d['cascade']['total']  # Cascade总LLM调用次数
            ax.bar(['原始 Agent', '三级架构'], [a_total, c_total],  # 柱状图
                   color=[C_AGENT, C_CASCADE], edgecolor='white', linewidth=0.5, width=0.5)
            for i, v in enumerate([a_total, c_total]):  # 遍历柱状条标注数值
                ax.text(i, v + max(a_total, c_total)*0.02, str(v),
                        ha='center', fontsize=13, fontweight='bold')
            ax.set_ylabel('LLM 调用次数', fontsize=12)
            ax.set_title('LLM 调用总量对比', fontsize=13, fontweight='bold', pad=10)
            ax.grid(True, alpha=0.2, axis='y')

            # 减少百分比
            reduction = d.get("comparison", {}).get("total_reduction", "N/A")  # 调用的减少百分比
            ax.text(0.5, 0.9, f'减少: {reduction}',
                    transform=ax.transAxes, ha='center', fontsize=12,
                    fontweight='bold', color='#27AE60')

        # (1,2) 墙钟时间
        ax = axes[0, 1]  # 右上子图
        if wall:
            a_time = wall['agent_total_seconds']  # Agent总时间（秒）
            c_time = wall['cascade_total_seconds']  # Cascade总时间（秒）
            a_per = wall['agent_per_ship_seconds']  # Agent每船平均时间（秒）
            c_per = wall['cascade_per_ship_seconds']  # Cascade每船平均时间（秒）

            x = np.arange(2)  # 两个系统的X轴位置
            width = 0.35  # 柱状图宽度
            ax.bar(x - width/2, [a_time, c_time], width, label='总时间',  # 总时间柱状图
                   color=[C_AGENT, C_CASCADE], edgecolor='white', linewidth=0.5, alpha=0.7)
            ax.bar(x + width/2, [a_per, c_per], width, label='每船时间',  # 每船时间柱状图
                   color=[C_AGENT_LIGHT, C_CASCADE_LIGHT],
                   edgecolor='white', linewidth=0.5, alpha=0.9)

            ax.set_xticks(x)  # 设置X轴刻度位置
            ax.set_xticklabels(['原始 Agent', '三级架构'], fontsize=11)  # 设置X轴刻度标签
            ax.set_ylabel('时间 (秒)', fontsize=12)
            ax.set_title('处理时间对比', fontsize=13, fontweight='bold', pad=10)
            ax.legend(fontsize=10)  # 图例
            ax.grid(True, alpha=0.2, axis='y')

            speedup = wall.get('speedup_ratio', 0)  # 加速比
            ax.text(0.5, 0.9, f'加速比: {speedup:.1f}x',
                    transform=ax.transAxes, ha='center', fontsize=12,
                    fontweight='bold', color='#27AE60')

        # (2,1) 容量存活
        ax = axes[1, 0]  # 左下子图
        if "capacity" in capacity_results:
            cap = capacity_results["capacity"]  # 容量测试数据
            detail = cap.get("survival_detail", {})  # 存活详情
            sizes_sorted = sorted(detail.items(), key=lambda x: int(x[0]))  # 按船舶数量排序
            ship_counts = [int(k) for k, _ in sizes_sorted]  # 各测试规模的船舶数量列表
            a_times = [v.get('agent_time_s', 0) for _, v in sizes_sorted]  # Agent各规模处理时间
            c_times = [v.get('cascade_time_s', 0) for _, v in sizes_sorted]  # Cascade各规模处理时间

            ax.plot(ship_counts, a_times, 'o-', color=C_AGENT, linewidth=2,  # Agent时间曲线
                    markersize=7, label='Agent')
            ax.plot(ship_counts, c_times, 's-', color=C_CASCADE, linewidth=2,  # Cascade时间曲线
                    markersize=7, label='Cascade')

            for s, a, c_ in zip(ship_counts, a_times, c_times):  # 遍历各规模数据点标注时间
                ax.annotate(f'{a:.0f}s', (s, a), textcoords="offset points",
                            xytext=(0, -15), fontsize=8, color=C_AGENT)
                ax.annotate(f'{c_:.0f}s', (s, c_), textcoords="offset points",
                            xytext=(0, 10), fontsize=8, color=C_CASCADE)

            ax.set_xlabel('船舶数量', fontsize=12)
            ax.set_ylabel('处理时间 (秒)', fontsize=12)
            ax.set_title('容量压力测试', fontsize=13, fontweight='bold', pad=10)
            ax.legend(fontsize=10)
            ax.grid(True, alpha=0.3)

            ax.text(0.5, 0.9,  # 在子图上方显示容量摘要
                    f'Cascade最大: {cap.get("cascade_max_capacity", "?")}艘'
                    f'  |  Agent最大: {cap.get("agent_max_capacity", "?")}艘'
                    f'  |  倍数: {cap.get("capacity_ratio", "?"):.0f}x',
                    transform=ax.transAxes, ha='center', fontsize=11,
                    fontweight='bold', color='#2C3E50',
                    bbox=dict(boxstyle='round,pad=0.3', facecolor='#F0F0F0', alpha=0.8))

        # (2,2) 汇总文本
        ax = axes[1, 1]  # 右下子图
        ax.axis('off')  # 隐藏坐标轴（纯文本区域）
        summary_lines = []  # 汇总文本行列表

        if "detail" in llm:
            d = llm["detail"]  # LLM调用详情
            summary_lines.append("【LLM 调用】")
            summary_lines.append(f"  Agent: {d['agent']['total']} 次")
            summary_lines.append(f"  Cascade: {d['cascade']['total']} 次")
            summary_lines.append(f"  减少: {d['comparison']['total_reduction']}")
            r = d['comparison'].get('wilcoxon_p', 0)  # Wilcoxon检验的p值
            summary_lines.append(f"  Wilcoxon p={r:.4f}"
                                 f" {'***' if d['comparison'].get('significant') else 'n.s.'}")
            summary_lines.append("")
        if wall:
            summary_lines.append("【处理速度】")
            summary_lines.append(f"  Agent: {wall['agent_total_seconds']:.0f}s")
            summary_lines.append(f"  Cascade: {wall['cascade_total_seconds']:.0f}s")
            summary_lines.append(f"  加速比: {wall['speedup_ratio']:.1f}x")
            summary_lines.append("")
        if "capacity" in capacity_results:
            cap = capacity_results["capacity"]  # 容量测试数据
            summary_lines.append("【容量测试】")
            summary_lines.append(f"  Agent 最大存活: {cap['agent_max_capacity']} 艘")
            summary_lines.append(f"  Cascade 最大存活: {cap['cascade_max_capacity']} 艘")
            summary_lines.append(f"  容量倍数: {cap['capacity_ratio']:.0f}x")

        ax.text(0.05, 0.95, '\n'.join(summary_lines),  # 在子图左上角显示汇总文本
                transform=ax.transAxes, fontsize=12,
                verticalalignment='top',
                bbox=dict(boxstyle='round,pad=1', facecolor='#F8F9FA', alpha=0.9))

        fig.suptitle('AIS 三级架构 vs 原始 Agent — 实验对比综合仪表板',  # 总标题
                     fontsize=16, fontweight='bold', y=1.01)
        plt.tight_layout()
        plt.savefig(path)
        plt.close(fig)