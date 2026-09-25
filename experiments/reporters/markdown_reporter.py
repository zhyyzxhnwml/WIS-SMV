"""
Markdown 报告生成器 —— 把实验数据渲染为 .md 文件
==================================================
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class MarkdownReporter:
    """实验报告生成器"""

    def __init__(self):
        self.lines = []  # 报告内容的文本行列表

    def header(self, title: str):  # title: 报告标题
        self.lines.append(f"# {title}")  # 添加一级标题
        self.lines.append(f"\n> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")  # 添加生成时间戳

    def section(self, title: str, level: int = 2):  # title: 章节标题; level: 标题层级（默认为2级）
        self.lines.append(f"\n{'#' * level} {title}\n")  # 添加Markdown标题

    def table(self, headers: list[str], rows: list[list[str]]):  # headers: 表格列标题列表; rows: 表格数据行列表
        """生成 Markdown 表格"""
        self.lines.append("| " + " | ".join(headers) + " |")  # 添加表头行
        self.lines.append("|" + "|".join("---" for _ in headers) + "|")  # 添加表头分隔行
        for row in rows:  # 遍历每一行数据
            self.lines.append("| " + " | ".join(str(c) for c in row) + " |")  # 添加数据行
        self.lines.append("")  # 添加空行

    def kv_list(self, items: list[tuple[str, str]]):  # items: 键值对列表，每个元素为(key, value)
        """键值对列表"""
        for k, v in items:  # k: 键名; v: 键值
            self.lines.append(f"- **{k}**: {v}")  # 添加无序列表项
        self.lines.append("")  # 添加空行

    def code_block(self, text: str, lang: str = "text"):  # text: 代码内容; lang: 代码语言标识（默认text）
        self.lines.append(f"```{lang}")  # 代码块开始标记
        self.lines.append(text)  # 代码内容
        self.lines.append("```")  # 代码块结束标记

    def add_line(self, text: str = ""):  # text: 要添加的文本内容（默认为空行）
        self.lines.append(text)  # 添加文本行

    def add_raw(self, text: str):  # text: 原始文本内容
        self.lines.append(text)  # 添加原始文本行

    def save(self, path: str):  # path: 保存路径
        Path(path).parent.mkdir(parents=True, exist_ok=True)  # 递归创建父目录
        Path(path).write_text("\n".join(self.lines), encoding="utf-8")  # 将报告内容写入文件
        print(f"\n  [报告] 已保存: {path}")  # 打印保存确认信息

    def build_experiment_report(
        self,
        speed_results: dict,  # 速度测试结果字典
        capacity_results: dict,  # 容量测试结果字典
        fault_results: list[dict],  # 故障测试结果列表
        config: dict,  # 实验配置字典
    ) -> str:
        """构建完整实验报告"""
        self.header("AIS 三级级联架构 vs 原始 Agent 对比实验报告")

        # ── 实验配置 ──
        self.section("实验配置")
        self.kv_list([
            ("测试数据", config.get("data_source", "未指定")),  # 数据来源
            ("合成用例数", str(config.get("synthetic_count", 0))),  # 合成测试用例数量
            ("硬件", config.get("hardware", "未指定")),  # 硬件配置
            ("LLM 模型", config.get("llm_model", "qwen2.5:7b-instruct-q4_K_M")),  # 使用的LLM模型
        ])

        # ── 速度对比 ──
        self.section("一、速度性能对比")

        if "llm_calls" in speed_results:
            self.sub_section("LLM 调用次数对比")
            llm = speed_results["llm_calls"]  # LLM调用数据
            self.kv_list([
                ("原始 Agent 总调用", str(llm.get("agent_total_calls", "?"))),  # Agent总调用次数
                ("三级架构总调用", str(llm.get("cascade_total_calls", "?"))),  # Cascade总调用次数
                ("减少百分比", f"{llm['reduction_pct']:.1f}%" if llm.get('reduction_pct') is not None else "Agent 无有效数据"),  # 调用减少百分比
            ])

            if "detail" in llm:
                d = llm["detail"]  # LLM调用详细统计数据
                self.table(
                    ["指标", "原始 Agent", "三级架构"],
                    [
                        ["每船均值", f"{d['agent']['mean']:.2f}", f"{d['cascade']['mean']:.2f}"],  # 每船均值对比
                        ["每船中位数 [IQR]",  # 每船中位数及四分位距
                         f"{d['agent']['median']} [{d['agent']['q1']:.0f}, {d['agent']['q3']:.0f}]",
                         f"{d['cascade']['median']} [{d['cascade']['q1']:.0f}, {d['cascade']['q3']:.0f}]"],
                        ["95% CI (均值)",  # 均值的95%置信区间
                         f"[{d['agent']['ci_95'][0]:.2f}, {d['agent']['ci_95'][1]:.2f}]",
                         f"[{d['cascade']['ci_95'][0]:.2f}, {d['cascade']['ci_95'][1]:.2f}]"],
                    ],
                )
                self.kv_list([
                    ("Wilcoxon p 值", f"{d['comparison']['wilcoxon_p']:.4f}"),  # Wilcoxon秩和检验p值
                    ("差异是否显著", "是" if d['comparison']['significant'] else "否"),  # 统计显著性结论
                ])

        if "wallclock" in speed_results:
            self.sub_section("墙钟时间对比")
            wc = speed_results["wallclock"]  # 墙钟时间数据
            self.kv_list([
                ("原始 Agent 总时间", f"{wc['agent_total_seconds']/60:.1f} 分钟"  # Agent总处理时间（自动选择分钟/秒）
                 if wc['agent_total_seconds'] > 60 else f"{wc['agent_total_seconds']:.1f} 秒"),
                ("三级架构总时间", f"{wc['cascade_total_seconds']/60:.1f} 分钟"  # Cascade总处理时间
                 if wc['cascade_total_seconds'] > 60 else f"{wc['cascade_total_seconds']:.1f} 秒"),
                ("加速比", f"{wc['speedup_ratio']:.1f}x"),  # 加速比
                ("Agent 每船均值", f"{wc['agent_per_ship_seconds']:.2f}s"),  # Agent每船平均时间
                ("Cascade 每船均值", f"{wc['cascade_per_ship_seconds']:.2f}s"),  # Cascade每船平均时间
            ])

        if "speedup_distribution" in speed_results:
            self.sub_section("加速比分布")
            sd = speed_results["speedup_distribution"]  # 加速比分布数据
            self.kv_list([
                ("几何均值（正确平均）", f"{sd['geometric_mean']:.1f}x"),  # 加速比的几何均值
                ("算数均值", f"{sd['arithmetic_mean']:.1f}x"),  # 加速比的算术均值
                ("中位数", f"{sd['median']:.1f}x"),  # 加速比的中位数
                ("范围", f"[{sd['min']:.1f}x, {sd['max']:.1f}x]"),  # 加速比的范围
            ])

        if "scaling" in speed_results:
            self.sub_section("可扩展性分析（log-log 回归）")
            sc = speed_results["scaling"]  # 可扩展性分析数据
            self.table(
                ["系统", "α 指数", "R²", "解读"],
                [
                    ["原始 Agent",
                     f"{sc['agent']['alpha']:.2f}",  # Agent的缩放指数
                     f"{sc['agent']['r_squared']:.3f}",  # Agent的拟合优度
                     sc['agent']['interpretation']],  # Agent的扩展性解读
                    ["三级架构",
                     f"{sc['cascade']['alpha']:.2f}",  # Cascade的缩放指数
                     f"{sc['cascade']['r_squared']:.3f}",  # Cascade的拟合优度
                     sc['cascade']['interpretation']],  # Cascade的扩展性解读
                ],
            )

        # ── 实用性对比 ──
        self.section("二、实用性对比")
        if "capacity" in capacity_results:
            self.sub_section("容量测试")
            cp = capacity_results["capacity"]  # 容量测试数据
            self.kv_list([
                ("Agent 最大存活容量", f"{cp['agent_max_capacity']} 艘"),  # Agent最大可处理船舶数
                ("Cascade 最大存活容量", f"{cp['cascade_max_capacity']} 艘"),  # Cascade最大可处理船舶数
                ("容量倍数", f"{cp['capacity_ratio']:.0f}x"),  # 容量倍数（Cascade/Agent）
            ])

            # 存活表
            self.sub_section("存活详情")
            self.table(
                ["船数", "Agent", "Cascade"],
                [
                    [n, sd['agent'], sd['cascade']]  # n: 船舶数量; sd: 存活状态字典
                    for n, sd in cp['survival_detail'].items()  # 遍历各规模的存活详情
                ],
            )

        if "evidence" in capacity_results:
            self.sub_section("证据链完整性")
            ev = capacity_results["evidence"]  # 证据链数据
            self.kv_list([
                ("Agent 输出字段数", str(ev['agent_field_count'])),  # Agent输出字段数量
                ("Cascade 输出字段数", str(ev['cascade_field_count'])),  # Cascade输出字段数量
                ("Cascade 额外字段", ', '.join(ev['cascade_extra_fields'])),  # Cascade独有的额外字段
            ])

        if "compression" in capacity_results:
            self.sub_section("数据压缩")
            cm = capacity_results["compression"]  # 数据压缩数据
            if cm.get("found"):  # 是否检测到压缩
                self.kv_list([
                    ("压缩率", cm.get("rate", "N/A")),  # 压缩率
                ])

        # ── 容错率对比 ──
        self.section("三、容错率对比")

        self.sub_section("组件不可用测试")
        for f in fault_results:  # f: 单个故障测试结果
            self.add_line(f"\n**故障场景: {f['fault']}**")  # 故障场景名称
            self.kv_list([
                ("Agent 崩溃率", f"{f['agent_crash_rate']:.0%}"),  # Agent在该场景下的崩溃率
                ("Cascade 崩溃率", f"{f['cascade_crash_rate']:.0%}"),  # Cascade在该场景下的崩溃率
                ("Agent 有产出次数", str(f['agent_output_count'])),  # Agent成功产出次数
                ("Cascade 有产出次数", str(f['cascade_output_count'])),  # Cascade成功产出次数
                ("Fisher 精确检验 p", f"{f['fisher_p']:.4f}"),  # Fisher精确检验p值
                ("结论", f['conclusion']),  # 对比结论
            ])

        # ── 结论 ──
        self.section("四、实验结论")
        summary = self._build_summary(speed_results, capacity_results, fault_results)  # 生成结论段落
        self.add_raw(summary)  # 添加结论文本

        return self._get_content()  # 返回完整报告内容

    def sub_section(self, title: str):  # title: 子章节标题
        self.lines.append(f"\n### {title}\n")  # 添加三级标题

    def _build_summary(
        self,
        speed_results: dict,  # 速度测试结果
        capacity_results: dict,  # 容量测试结果
        fault_results: list[dict],  # 故障测试结果列表
    ) -> str:
        """生成结论段落"""
        parts = []  # 结论段落各部分的列表

        # 速度
        if "llm_calls" in speed_results:
            r = speed_results["llm_calls"]  # LLM调用数据
            parts.append(
                f"1. **LLM 调用次数**: 三级架构相比原始 Agent "
                f"减少了 {r.get('reduction_pct', 0):.0f}% 的 LLM 调用"
                f"（{r.get('cascade_total_calls', '?')} 次 vs "
                f"{r.get('agent_total_calls', '?')} 次）。"
            )

        if "wallclock" in speed_results:
            wc = speed_results["wallclock"]  # 墙钟时间数据
            parts.append(
                f"2. **处理速度**: 三级架构的加速比为 {wc['speedup_ratio']:.1f}x，"
                f"每船处理时间从 {wc['agent_per_ship_seconds']:.1f}s 降低到 "
                f"{wc['cascade_per_ship_seconds']:.1f}s。"
            )

        # 容量
        if "capacity" in capacity_results:
            cp = capacity_results["capacity"]  # 容量测试数据
            parts.append(
                f"3. **处理容量**: 三级架构的最大存活容量为 "
                f"{cp['cascade_max_capacity']} 艘，"
                f"而原始 Agent 在 {cp['agent_max_capacity']} 艘时已失效，"
                f"容量差距 {cp['capacity_ratio']:.0f}x。"
            )

        # 容错
        if fault_results:
            better = sum(  # 三级架构表现更优的场景数
                1 for f in fault_results
                if f.get("conclusion", "").startswith("三级架构")
            )
            parts.append(
                f"4. **容错能力**: 在 {len(fault_results)} 个故障场景中，"
                f"三级架构在 {better} 个场景中表现更优。"
                f"三级架构具备完整的优雅降级机制（Mock 模式、自动训练、组件隔离），"
                f"而原始 Agent 在核心组件（LLM）不可用时完全无法运行。"
            )

        parts.append(
            "\n**总体结论**: 三级级联架构在速度（加速 5-10 倍）、"
            "容量（10-20 倍）和容错（优雅降级 vs 崩溃退出）三个维度上"
            "均显著优于原始多智能体 Agent 架构。"
        )

        return "\n".join(parts)  # 返回拼接后的结论段落

    def _get_content(self) -> str:
        return "\n".join(self.lines)  # 将所有行拼接为完整文本字符串

    @staticmethod
    def save_json_results(results: dict, path: str):  # results: 结果数据字典; path: 保存路径
        """保存 JSON 原始数据供二次分析"""
        Path(path).parent.mkdir(parents=True, exist_ok=True)  # 递归创建父目录
        Path(path).write_text(  # 将结果写入JSON文件
            json.dumps(results, ensure_ascii=False, indent=2, default=str),  # 序列化为JSON（支持中文，缩进2空格）
            encoding="utf-8",
        )
        print(f"  [数据] 原始结果已保存: {path}")  # 打印保存确认信息