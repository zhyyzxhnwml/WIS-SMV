"""
shipclaw/render_blocks.py — 智能体输出渲染块协议
=================================================

【这个文件做什么？】
  定义智能体输出的"渲染块"（Render Block）协议。每个 block 是一个
  自描述的数据结构，前端按 kind 分发到对应渲染器（ECharts/表格/文档/...）。

【核心约束 —— 只内联渲染，不落文件】
  所有 block 只携带数据与渲染说明，不产生任何文件/下载路径。
  这是级联监管者智能体与文档/表格/画图 MCP 工具的边界：
  工具负责"组数据"，前端负责"画出来"，全程不落地。

【Block 类型】
  - metric   指标卡片：一组 key-value + 提示
  - chart    图表：bar/line/pie/radar + 数据
  - table    表格：列定义 + 行数据
  - doc      文档：分节排版（标题 + 段落 + 列表）
  - map      地图轨迹：轨迹点 + 事件标记
  - evidence 违规证据链：复用 EvidenceCard 数据结构
  - text     纯文本（兜底）
"""

from __future__ import annotations

from typing import Any, Optional


# ── 基础构造器 ──────────────────────────────────────────────────────

def metric_block(title: str, items: list[dict[str, Any]], *,
                note: str = "") -> dict[str, Any]:
    """指标卡片。

    items: [{"label": "船舶数", "value": 600, "hint": "辖区内"}, ...]
    """
    return {
        "kind": "metric",
        "title": title,
        "note": note,
        "spec": {"items": items},
    }


def chart_block(title: str, chart_type: str, *,
                x: list[str], series: list[dict[str, Any]],
                unit: str = "", y_name: str = "",
                subtitle: str = "") -> dict[str, Any]:
    """图表块。

    chart_type: "bar" | "line" | "pie" | "radar"
    x: 类目轴数据
    series: [{"name": "活跃", "data": [85, 92, ...], "color": "#0c8ce9"}, ...]
    """
    assert chart_type in ("bar", "line", "pie", "radar"), f"未知 chart_type: {chart_type}"
    return {
        "kind": "chart",
        "title": title,
        "note": subtitle,
        "spec": {
            "chart_type": chart_type,
            "x": x,
            "series": series,
            "unit": unit,
            "y_name": y_name,
        },
    }


def table_block(title: str, columns: list[dict[str, str]], rows: list[list[Any]], *,
               note: str = "", max_height: Optional[int] = None) -> dict[str, Any]:
    """表格块。

    columns: [{"key": "mmsi", "label": "船舶 MMSI", "align": "left"}, ...]
    rows: [["412345678", "违规", "2026-07-25"], ...]  (与 columns 顺序对齐)
    """
    return {
        "kind": "table",
        "title": title,
        "note": note,
        "spec": {
            "columns": columns,
            "rows": rows,
            "max_height": max_height,
        },
    }


def doc_block(title: str, sections: list[dict[str, Any]], *,
             note: str = "") -> dict[str, Any]:
    """文档块（分节排版，纯展示，不是文件）。

    sections: [
      {"heading": "一、总体情况", "paragraphs": ["..."], "list": ["..."]},
      ...
    ]
    """
    return {
        "kind": "doc",
        "title": title,
        "note": note,
        "spec": {"sections": sections},
    }


def map_track_block(title: str, track: list[dict[str, Any]], *,
                   events: Optional[list[dict[str, Any]]] = None,
                   center: Optional[tuple[float, float]] = None) -> dict[str, Any]:
    """地图轨迹块。

    track: [{"lat": 29.58, "lon": 106.66, "speed": 5.2, "timestamp": "..."}, ...]
    events: [{"lat": ..., "lon": ..., "type_label": "超速", "start_time": "..."}, ...]
    center: (lon, lat) 地图中心
    """
    return {
        "kind": "map",
        "title": title,
        "note": "",
        "spec": {
            "track": track or [],
            "events": events or [],
            "center": center,
        },
    }


def evidence_block(data: dict[str, Any]) -> dict[str, Any]:
    """违规证据链块（前端用 EvidenceCard 渲染）。

    data: 与 ViolationEvidenceSkill._build_evidence_data 同构
    """
    return {
        "kind": "evidence",
        "title": f"MMSI {data.get('mmsi', '')} 违规证据",
        "note": "",
        "spec": data,
    }


def text_block(text: str) -> dict[str, Any]:
    """纯文本块（兜底）。"""
    return {
        "kind": "text",
        "title": "",
        "note": "",
        "spec": {"text": text},
    }


# ── 便捷组合构造器 ──────────────────────────────────────────────────

def overview_blocks(user_id: str) -> list[dict[str, Any]]:
    """组合"辖区总览"的一组 block：指标卡 + 违规类型饼图 + 趋势折线 + 表格。"""
    from .data_access import (
        q_overview, q_violation_types, q_daily_trend, q_violation_list,
    )

    ov = q_overview(user_id)
    blocks: list[dict[str, Any]] = []

    # 指标卡
    blocks.append(metric_block("辖区总览", [
        {"label": "船舶总数", "value": ov["ships"], "hint": "辖区内去重"},
        {"label": "违规船舶", "value": ov["violation_ships"], "hint": "已检出"},
        {"label": "违规次数", "value": ov["violations"], "hint": "累计"},
        {"label": "检测记录", "value": ov["total_records"], "hint": "总条数"},
    ]))

    # 违规类型分布（饼图）
    types = q_violation_types(user_id)
    if types:
        pie_data = [{"name": t["label"], "value": t["count"]} for t in types]
        blocks.append(chart_block(
            "违规类型分布", "pie",
            x=[], series=[{"name": "违规", "data": pie_data}],
            subtitle="按规范码聚合",
        ))

    # 日趋势（折线）
    trend = q_daily_trend(user_id)
    if trend:
        blocks.append(chart_block(
            "违规日趋势", "line",
            x=[r["day"][5:10] for r in trend],
            series=[{"name": "违规数", "data": [r["count"] for r in trend], "color": "#ef5350"}],
            unit="起",
        ))

    # 最近违规表
    rows = q_violation_list(user_id, 8)
    if rows:
        blocks.append(table_block(
            "最近违规记录",
            [
                {"key": "mmsi", "label": "MMSI", "align": "left"},
                {"key": "violation_type", "label": "违规类型", "align": "left"},
                {"key": "risk_level", "label": "风险", "align": "center"},
                {"key": "analysis_time", "label": "时间", "align": "center"},
            ],
            [[r["mmsi"], r["violation_type"], r["risk_level"], r["analysis_time"]] for r in rows],
        ))

    return blocks
