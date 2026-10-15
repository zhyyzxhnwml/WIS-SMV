"""
ais_accelerator/flow.py — 水域流向场与流速场
==============================================

【这个文件做什么？】
  从**全船队**的历史轨迹一次性统计出两样"背景知识"，供单船检测器按格 O(1) 查询：

  - 流向场（direction field）：每个格网内船舶的**主导航向**，最多保留两个。
  - 流速场（speed field）：每个格网内船舶的**主流速分位**。

【为什么需要它】
  超速与逆向航行的既有判据都依赖"区域"——`regulations.json` 里人工画的 zones。
  可那里的 3 个区全在 38°N（渤海示例），真实数据在 24–27°N（福建沿海），**零重叠**，
  于是"区域超速"这个判据从未触发过。改成从数据本身学出参照值，就不依赖人工画区域。

【为什么主导航向可能是**两个**】
  分道通航制（TSS）是双向航道，对向车道是合法方向。只取一个主导航向会把
  整条对向车道判成"逆行"——实测该口径下 91% 的船被误判。所以这里保留最多
  `flow_top_modes` 个互相分隔的模态，判定"逆向"必须对**所有**模态都逆向。

【设计原则】
  与 `water.WaterChecker` 同构：**构建一次、按格查询**。
  每格只保留**直方图**而非原始点，所以 93.7 万点也只需几 MB，不常驻全部 AISPoint。
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

import math  # 取整与角度运算
from typing import TYPE_CHECKING, Iterable, Optional  # 类型注解

from .config import AcceleratorConfig  # 加速器配置
from .geometry import circular_angle_diff  # 两个角度的最小差值（度）

if TYPE_CHECKING:  # 仅类型检查时导入，避免运行期循环依赖
    from .models import AISPoint  # AIS数据点


_COURSE_BIN_DEGREES = 5.0  # 航向直方图箱宽（360/5 = 72 箱）
_COURSE_BINS = int(360.0 / _COURSE_BIN_DEGREES)  # 航向箱数
_COURSE_SMOOTH_HALF = 2  # 环形平滑半窗（±2 箱 = ±10°）
_SPEED_BIN_KNOTS = 0.5  # 速度直方图箱宽（节）
_SPEED_MAX_BIN = 119  # 速度封顶箱（60 节），以上并入末箱


class FlowField:  # 水域流向场 + 流速场
    """从全船队轨迹统计出的"水域主流向 + 主流速"参照场。"""

    def __init__(  # 初始化
        self,
        config: AcceleratorConfig,  # 加速器配置
        course_hist: Optional[dict[tuple[int, int], dict[int, int]]] = None,  # 每格航向直方图
        speed_hist: Optional[dict[tuple[int, int], dict[int, int]]] = None,  # 每格速度直方图
    ) -> None:
        self.cell_degrees = config.flow_cell_degrees  # 格网边长（度）
        self.min_samples = config.flow_min_samples  # 格网最小样本量
        self.top_modes = config.flow_top_modes  # 每格最多保留的主导航向数
        self.mode_separation = config.flow_mode_separation_degrees  # 两模态最小夹角（度）
        self.percentile = config.flow_speed_percentile  # 流速参照取的分位
        self._course_hist = course_hist or {}  # 航向直方图
        self._speed_hist = speed_hist or {}  # 速度直方图
        self._modes: dict[tuple[int, int], list[float]] = {}  # 模态缓存（格 → 主导航向列表）
        self._speed_ref: dict[tuple[int, int], Optional[float]] = {}  # 流速参照缓存

    # ── 构建 ──

    @classmethod
    def build_from_tracks(  # 从全船队轨迹一次性构建
        cls,
        tracks: Iterable[tuple[str, list["AISPoint"]]],  # (mmsi, 轨迹点) 可迭代对象
        config: AcceleratorConfig,  # 加速器配置
    ) -> "FlowField":
        """扫一遍全船队轨迹，按格网累加航向与速度直方图。

        只保留直方图、不保留原始点：fzais1009 有 53.5 万运动点，
        常驻 Pydantic 对象会占数百 MB，直方图则是每格几十个整数。
        """
        course_hist: dict[tuple[int, int], dict[int, int]] = {}  # 航向直方图累加器
        speed_hist: dict[tuple[int, int], dict[int, int]] = {}  # 速度直方图累加器
        cell = config.flow_cell_degrees  # 格网边长
        min_speed = config.flow_min_speed_knots  # 运动点速度下限
        for _mmsi, points in tracks:  # 逐船遍历
            for p in points:  # 逐点遍历
                if p.speed < min_speed:  # 静止/低速点
                    continue  # COG 在静止时是噪声，不参与流向统计
                key = (int(math.floor(p.lat / cell)), int(math.floor(p.lon / cell)))  # 格网键
                cb = int((p.course % 360.0) // _COURSE_BIN_DEGREES)  # 航向箱号
                ch = course_hist.get(key)  # 取该格航向直方图
                if ch is None:  # 首次遇到该格
                    ch = course_hist[key] = {}  # 新建直方图
                ch[cb] = ch.get(cb, 0) + 1  # 计数累加
                sb = min(int(p.speed // _SPEED_BIN_KNOTS), _SPEED_MAX_BIN)  # 速度箱号（封顶）
                sh = speed_hist.get(key)  # 取该格速度直方图
                if sh is None:  # 首次遇到该格
                    sh = speed_hist[key] = {}  # 新建直方图
                sh[sb] = sh.get(sb, 0) + 1  # 计数累加
        return cls(config, course_hist, speed_hist)  # 返回构建好的场

    # ── 查询 ──

    def _cell_of(self, lon: float, lat: float) -> tuple[int, int]:  # 计算经纬度所属格网键
        return (  # 返回（纬度格, 经度格）
            int(math.floor(lat / self.cell_degrees)),  # 纬度方向格号
            int(math.floor(lon / self.cell_degrees)),  # 经度方向格号
        )

    def dominant_courses(self, lon: float, lat: float) -> list[float]:  # 查询该格主导航向
        """返回该格网的主导航向（按平滑计数降序，最多 `flow_top_modes` 个）。

        样本量不足 `flow_min_samples` 时返回 **空列表**——注意这**不等于**"没有主流向
        所以合规"，而是"无从判断"。调用方必须据此跳过判定，不能当成合规。
        """
        key = self._cell_of(lon, lat)  # 格网键
        cached = self._modes.get(key)  # 命中缓存
        if cached is not None:  # 已算过
            return cached  # 直接返回
        modes = self._extract_modes(self._course_hist.get(key))  # 提取模态
        self._modes[key] = modes  # 写缓存
        return modes  # 返回结果

    def _extract_modes(self, hist: Optional[dict[int, int]]) -> list[float]:  # 从直方图提取主导航向
        if not hist:  # 无样本
            return []  # 无模态
        if sum(hist.values()) < self.min_samples:  # 样本量不足
            return []  # 无模态（无从判断，交给调用方跳过）
        # 环形平滑：±2 箱累加。不做平滑的话，同一股船流若正好压在箱边界上，
        # 会被劈成两个都不到样本量的半峰，整格被判为"无主导航向"而全部漏检。
        smooth = [  # 平滑后的环形计数
            sum(  # 滑窗累加
                hist.get((b + d) % _COURSE_BINS, 0)  # 环形取箱
                for d in range(-_COURSE_SMOOTH_HALF, _COURSE_SMOOTH_HALF + 1)  # 滑窗范围
            )
            for b in range(_COURSE_BINS)  # 遍历每一箱
        ]
        picked: list[float] = []  # 已选模态
        for b in sorted(range(_COURSE_BINS), key=lambda i: -smooth[i]):  # 按平滑计数降序取峰
            if smooth[b] <= 0:  # 计数归零，后面全是 0
                break  # 结束
            center = b * _COURSE_BIN_DEGREES + _COURSE_BIN_DEGREES / 2.0  # 箱中心角度
            if all(  # 与已选模态都足够分离
                circular_angle_diff(center, p) >= self.mode_separation  # 夹角达标
                for p in picked  # 遍历已选模态
            ):
                picked.append(center)  # 收下该模态
                if len(picked) >= self.top_modes:  # 已够数
                    break  # 结束
        return picked  # 返回模态列表

    def course_deviation(self, lon: float, lat: float, course: float) -> Optional[float]:  # 航向偏离度
        """本点航向与所在格网**最近**主导航向的夹角（度）；无参照时返回 None。

        取**最小**夹角是刻意的：只要顺着任一合法车道走就不算逆向。
        因此"逆向航行"的判据是 `course_deviation(...) >= 阈值`
        ——即对**所有**主导航向都逆向，而不是"偏离了某一个"。
        """
        modes = self.dominant_courses(lon, lat)  # 取该格模态
        if not modes:  # 无参照
            return None  # 调用方应跳过判定
        return min(circular_angle_diff(course, m) for m in modes)  # 与最近模态的夹角

    def speed_reference(self, lon: float, lat: float) -> Optional[float]:  # 查询该格主流速
        """该格网的流速参照值（`flow_speed_percentile` 分位）；样本不足返回 None。

        取**分位**而非均值：均值会被超速船自己抬高，从而把超速判据抬到永不触发。
        """
        key = self._cell_of(lon, lat)  # 格网键
        if key in self._speed_ref:  # 命中缓存
            return self._speed_ref[key]  # 直接返回
        ref = self._percentile(self._speed_hist.get(key), self.percentile)  # 算分位
        self._speed_ref[key] = ref  # 写缓存
        return ref  # 返回结果

    def _percentile(self, hist: Optional[dict[int, int]], q: float) -> Optional[float]:  # 直方图分位
        if not hist:  # 无样本
            return None  # 无参照
        total = sum(hist.values())  # 总样本量
        if total < self.min_samples:  # 样本量不足
            return None  # 无参照
        target = total * q  # 目标累计数
        cumulative = 0  # 累计计数
        for b in sorted(hist):  # 从低速到高速累加
            cumulative += hist[b]  # 累加该箱
            if cumulative >= target:  # 达到目标分位
                return b * _SPEED_BIN_KNOTS + _SPEED_BIN_KNOTS / 2.0  # 返回箱中心值
        return (max(hist) + 1) * _SPEED_BIN_KNOTS  # 兜底：末箱上界

    # ── 自检 ──

    def stats(self) -> dict[str, float]:  # 规模统计（供验证脚本打印）
        """场规模摘要：可用格网数、模态分布、流速参照范围。"""
        with_modes = 0  # 有主导航向的格数
        dual_modes = 0  # 双模态格数（分道通航制的特征）
        for key, hist in self._course_hist.items():  # 遍历所有有样本的格
            modes = self._extract_modes(hist)  # 提取模态
            if modes:  # 有主导航向
                with_modes += 1  # 计数
                if len(modes) >= 2:  # 双模态
                    dual_modes += 1  # 计数
        refs = [  # 各格流速参照
            self._percentile(h, self.percentile)  # 计算分位
            for h in self._speed_hist.values()  # 遍历速度直方图
        ]
        refs = [r for r in refs if r is not None]  # 剔除样本不足的格
        return {  # 返回统计字典
            "cell_degrees": self.cell_degrees,  # 格网边长
            "min_samples": self.min_samples,  # 样本量下限
            "cells": len(self._course_hist),  # 有航向样本的格数
            "cells_with_modes": with_modes,  # 有主导航向的格数
            "cells_dual_mode": dual_modes,  # 双模态格数
            "speed_ref_cells": len(refs),  # 有流速参照的格数
            "speed_ref_min": round(min(refs), 2) if refs else 0.0,  # 流速参照最小值
            "speed_ref_max": round(max(refs), 2) if refs else 0.0,  # 流速参照最大值
        }
