"""
shipclaw/render_skills.py — 三个渲染型工具（MCP 工具）
========================================================

  画图工具  chart_render   → chart_block（按意图选 bar/line/pie）
  写表格    table_render   → table_block + metric_block
  写文档    doc_render     → doc_block

  所有工具只产出结构化渲染块供前端内联渲染，不生成任何文件。
"""

from __future__ import annotations

from typing import Any

from .data_access import (
    q_overview, q_violation_types, q_risk_distribution, q_daily_trend,
    q_violation_list, q_ship_list, q_top_violators,
)
from .render_blocks import (
    metric_block, chart_block, table_block, doc_block, text_block,
)


# ── 画图工具 ──────────────────────────────────────────────────────

class ChartToolSkill:
    """画图工具——根据用户意图生成图表渲染块（bar/line/pie）。

    产出 block 仅供前端内联渲染，不生成任何图片文件。
    """
    name = "chart_render"
    label = "智能画图"
    description = "根据你的需求生成柱状图/折线图/饼图，对话内直接展示，不生成图片文件"
    icon = "📊"
    params = [
        {"name": "chart_type", "label": "图表类型", "description": "柱状图/折线图/饼图/对比图", "required": False},
    ]
    min_level = 3
    exp_reward = 12

    def match(self, message: str) -> float:
        msg = message.lower()
        # 画图/图表/可视化关键词
        if any(kw in msg for kw in ["画图", "画个图", "生成图", "图表", "柱状图", "折线图",
                                     "饼图", "可视化", "画一张", "出个图"]):
            # 排除纯"看看图"（无数据意图）
            if any(kw in msg for kw in ["违规", "船舶", "趋势", "分布", "统计", "对比", "类型", "风险"]):
                return 0.88
            return 0.7
        if any(kw in msg for kw in ["画一个", "画份", "来张图", "做个图", "呈现"]):
            return 0.75
        return 0.0

    def execute(self, message: str, context: dict) -> dict:
        user_id = context.get("user_id", "system")
        msg = message.lower()

        # 判断图表类型
        chart_type = "bar"
        if any(kw in msg for kw in ["饼图", "占比", "分布", "比例"]):
            chart_type = "pie"
        elif any(kw in msg for kw in ["折线图", "趋势", "变化", "走势"]):
            chart_type = "line"
        elif any(kw in msg for kw in ["柱状图", "对比", "排名"]):
            chart_type = "bar"

        blocks = []

        # 根据意图选数据
        if any(kw in msg for kw in ["违规", "类型", "分布", "饼图"]):
            # 违规类型分布
            types = q_violation_types(user_id)
            if chart_type == "pie" and types:
                pie_data = [{"name": t["label"], "value": t["count"]} for t in types]
                blocks.append(chart_block(
                    "违规类型分布", "pie",
                    x=[], series=[{"name": "违规船舶", "data": pie_data}],
                    subtitle="按规范码聚合",
                ))
            elif types:
                blocks.append(chart_block(
                    "违规类型分布", chart_type,
                    x=[t["label"] for t in types],
                    series=[{"name": "违规数", "data": [t["count"] for t in types], "color": "#ef5350"}],
                    unit="起",
                ))
        elif any(kw in msg for kw in ["风险", "等级"]):
            # 风险等级分布
            risks = q_risk_distribution(user_id)
            color_map = {"high": "#ef5350", "mid": "#f5a524", "low": "#0c8ce9"}
            blocks.append(chart_block(
                "风险等级分布", chart_type if chart_type != "line" else "bar",
                x=[r["label"] for r in risks],
                series=[{"name": "违规数", "data": [r["count"] for r in risks],
                         "color": "#f5a524"}],
                unit="起",
            ))
        elif any(kw in msg for kw in ["趋势", "变化", "走势", "折线"]):
            trend = q_daily_trend(user_id)
            blocks.append(chart_block(
                "违规日趋势", "line",
                x=[r["day"][5:10] for r in trend],
                series=[{"name": "违规数", "data": [r["count"] for r in trend], "color": "#ef5350"}],
                unit="起",
            ))
        else:
            # 默认：总览对比
            ov = q_overview(user_id)
            blocks.append(chart_block(
                "辖区概况对比", "bar",
                x=["船舶总数", "违规船舶", "违规次数"],
                series=[{"name": "数量", "data": [ov["ships"], ov["violation_ships"], ov["violations"]],
                         "color": "#0c8ce9"}],
            ))

        if not blocks:
            return {"text": "抱歉，当前辖区暂无足够数据生成图表。", "blocks": []}

        summary = "以下是为你生成的图表（对话内直接展示）："
        return {"text": summary, "blocks": blocks}


# ── 写表格工具 ────────────────────────────────────────────────────

class TableToolSkill:
    """写表格工具——根据用户意图生成结构化表格。

    产出 block 仅供前端内联渲染，不生成任何表格文件。
    """
    name = "table_render"
    label = "智能表格"
    description = "生成数据表格（违规记录/船舶列表/统计汇总），对话内直接展示，不生成表格文件"
    icon = "📋"
    params = [
        {"name": "table_type", "label": "表格类型", "description": "违规记录/船舶列表/统计汇总", "required": False},
    ]
    min_level = 3
    exp_reward = 12

    def match(self, message: str) -> float:
        msg = message.lower()
        if any(kw in msg for kw in ["表格", "列表", "清单", "明细", "汇总", "排行", "排名", "一览表"]):
            if any(kw in msg for kw in ["违规", "船舶", "船", "统计", "记录", "情况"]):
                return 0.85
            return 0.65
        if any(kw in msg for kw in ["整理成表", "做成表格", "列出来", "统计一下", "汇总一下"]):
            return 0.75
        return 0.0

    def execute(self, message: str, context: dict) -> dict:
        user_id = context.get("user_id", "system")
        msg = message.lower()
        blocks = []

        if any(kw in msg for kw in ["违规", "记录"]):
            rows = q_violation_list(user_id, 15)
            risk_label = {"high": "高风险", "mid": "中风险", "low": "低风险"}
            if rows:
                blocks.append(metric_block("违规统计", [
                    {"label": "违规记录数", "value": str(len(rows)), "hint": "最近"},
                ]))
                blocks.append(table_block(
                    "违规记录",
                    [
                        {"key": "mmsi", "label": "MMSI", "align": "left"},
                        {"key": "violation_type", "label": "违规类型", "align": "left"},
                        {"key": "risk_level", "label": "风险等级", "align": "center"},
                        {"key": "analysis_time", "label": "检测时间", "align": "center"},
                    ],
                    [[r["mmsi"], r["violation_type"], risk_label.get(r["risk_level"], r["risk_level"]),
                      r["analysis_time"]] for r in rows],
                ))
            # 多次违规船
            top = q_top_violators(user_id, 5)
            if top:
                blocks.append(table_block(
                    "多次违规船舶",
                    [
                        {"key": "mmsi", "label": "MMSI", "align": "left"},
                        {"key": "count", "label": "违规次数", "align": "center"},
                    ],
                    [[t["mmsi"], t["count"]] for t in top],
                ))

        elif any(kw in msg for kw in ["船舶", "船"]):
            ships = q_ship_list(user_id, 15)
            if ships:
                blocks.append(metric_block("船舶统计", [
                    {"label": "船舶数", "value": str(len(ships)), "hint": "最近"},
                ]))
                blocks.append(table_block(
                    "船舶列表",
                    [
                        {"key": "mmsi", "label": "MMSI", "align": "left"},
                        {"key": "status", "label": "状态", "align": "center"},
                        {"key": "analysis_time", "label": "最近检测", "align": "center"},
                    ],
                    [[s["mmsi"], s["status"], s["analysis_time"]] for s in ships],
                ))
        else:
            # 默认：总览
            ov = q_overview(user_id)
            blocks.append(metric_block("辖区总览", [
                {"label": "船舶总数", "value": ov["ships"], "hint": "辖区内去重"},
                {"label": "违规船舶", "value": ov["violation_ships"], "hint": "已检出"},
                {"label": "违规次数", "value": ov["violations"], "hint": "累计"},
                {"label": "检测记录", "value": ov["total_records"], "hint": "总条数"},
            ]))

        if not blocks:
            return {"text": "抱歉，当前辖区暂无数据可生成表格。", "blocks": []}

        return {"text": "以下是为你生成的表格（对话内直接展示）：", "blocks": blocks}


# ── 写文档工具 ────────────────────────────────────────────────────

class DocToolSkill:
    """写文档工具——生成分节排版的数据文档。

    产出 block 仅供前端内联渲染，不生成任何文档文件（.docx/.pdf）。
    """
    name = "doc_render"
    label = "智能文档"
    description = "生成数据报告/情况说明/分析总结，对话内直接展示，不生成文档文件"
    icon = "📝"
    params = [
        {"name": "doc_type", "label": "文档类型", "description": "数据报告/情况说明/分析总结", "required": False},
    ]
    min_level = 3
    exp_reward = 14

    def match(self, message: str) -> float:
        msg = message.lower()
        if any(kw in msg for kw in ["文档", "报告", "总结", "说明", "汇报", "简报", "情况说明",
                                     "写一份", "写个报告", "写一篇", "出一份"]):
            return 0.82
        if any(kw in msg for kw in ["报告一下", "总结一下", "说明一下", "汇报一下"]):
            return 0.7
        return 0.0

    def execute(self, message: str, context: dict) -> dict:
        user_id = context.get("user_id", "system")
        msg = message.lower()

        ov = q_overview(user_id)
        types = q_violation_types(user_id)
        risks = q_risk_distribution(user_id)
        trend = q_daily_trend(user_id)
        top_v = q_top_violators(user_id, 3)

        sections = []

        sections.append({
            "heading": "一、辖区概况",
            "paragraphs": [
                f"当前辖区共扫描 {ov['ships']} 艘船舶，累计检测 {ov['total_records']} 条记录。",
                f"检出违规船舶 {ov['violation_ships']} 艘，违规事件 {ov['violations']} 起。",
            ],
        })

        if types:
            type_lines = "、".join(f"{t['label']}（{t['count']}起）" for t in types[:6])
            sections.append({
                "heading": "二、违规类型分布",
                "paragraphs": [f"违规类型主要集中在：{type_lines}。"],
                "list": [f"{t['label']}：{t['count']} 起" for t in types],
            })

        if risks:
            risk_lines = "、".join(f"{r['label']}（{r['count']}起）" for r in risks)
            sections.append({
                "heading": "三、风险等级分布",
                "paragraphs": [f"风险等级分布为：{risk_lines}。"],
            })

        if trend:
            peak = max(trend, key=lambda r: r["count"])
            sections.append({
                "heading": "四、时间趋势",
                "paragraphs": [
                    f"近 {len(trend)} 天内，违规事件共 {sum(r['count'] for r in trend)} 起。",
                    f"峰值出现在 {peak['day']}，当日 {peak['count']} 起。",
                ],
            })

        if top_v:
            recid_lines = "、".join(f"{t['mmsi']}（{t['count']}次）" for t in top_v)
            sections.append({
                "heading": "五、多次违规船舶",
                "paragraphs": [f"多次违规船舶：{recid_lines}。"],
            })

        blocks = [
            doc_block("辖区数据报告", sections, note="内联展示，未生成文件"),
        ]

        return {"text": "以下是为你生成的数据报告（对话内直接展示）：", "blocks": blocks}
