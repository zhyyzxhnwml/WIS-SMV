# -*- coding: utf-8 -*-
"""诊断：0/2 是真漏报，还是区域引擎/几何根本没生效。

分三步，每一步都能独立失败：
  1. 区域规则加载 → zones 数量对得上吗
  2. 几何 → 真值坐标点落在系统自己的 point_in_polygon 里吗
  3. 判据 → 那些点的速度是否都 > low_speed_enter_knots（3 节）

跑完可删。
"""
from __future__ import annotations

import csv
import gzip
import json
import sys
from pathlib import Path

ROOT = Path(r"E:\AIS")
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ais_accelerator.config import AcceleratorConfig          # noqa: E402
from ais_accelerator.rules import JsonRuleProvider            # noqa: E402
from ais_accelerator.geometry import point_in_polygon         # noqa: E402

REG = ROOT / "data/logs/paper2/regulations_hawaii.json"
AIS = ROOT / "data/public/marinecadastre/hawaii"
GT = json.loads((ROOT / "data/logs/real_violations.json").read_text(encoding="utf-8"))

cfg = AcceleratorConfig(regulations_path=REG)
prov = JsonRuleProvider(str(REG))
print(f"[1] 规则加载：{len(prov.zones)} 个区域")
print(f"    low_speed_enter_knots = {cfg.low_speed_enter_knots}")
print(f"    fishing_min_minutes   = {cfg.fishing_min_minutes}")
print(f"    fishing_min_points    = {cfg.fishing_min_points}")

# [2] 几何：直接用真值里的违规点坐标问系统
print("\n[2] 几何自检（用真值违规点的实际坐标）")
by_day: dict[str, list[tuple[str, float, float, float]]] = {}
for v in GT["violations"]:
    day = v["t0"][:10]
    mmsi = v["mmsi"]
    path = AIS / f"{day}.csv.gz"
    if not path.exists():
        print(f"    {day} 数据缺失")
        continue
    pts = []
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            if str(r["MMSI"]).strip() != mmsi:
                continue
            try:
                lon, lat, sog = float(r["LON"]), float(r["LAT"]), float(r["SOG"] or 0)
            except ValueError:
                continue
            pts.append((r["BaseDateTime"], lon, lat, sog))
    by_day.setdefault(day, []).extend(pts)

    n_in = 0
    spds = []
    for ts, lon, lat, sog in pts:
        zs = prov.zones_at(lat, lon)
        if zs:
            n_in += 1
            spds.append(sog)
    print(f"    {mmsi} @ {day}：全天 {len(pts)} 点，落在系统识别到的区域内 {n_in} 点")
    if spds:
        low = [s for s in spds if s <= cfg.low_speed_enter_knots]
        print(f"       区内速度 min={min(spds):.2f} max={max(spds):.2f} 节；"
              f"≤{cfg.low_speed_enter_knots} 节的点数 = {len(low)}")

# [3] 反向对照：把 Kaho'olawe 临时改成"禁入"，看 restricted_area_entry 会不会出
print("\n[3] 反向对照：把 Kaho'olawe 改成 entry_allowed=false 再查一次")
kaho = [z for z in prov.zones if "Kaho" in str(z.get("name"))]
if kaho:
    ring = kaho[0]["boundary"]
    hit = [p for p in by_day.get("2018-07-22", [])
           if str(p[0]).startswith("2018-07-22") and point_in_polygon(p[1], p[2], ring)]
    print(f"    Kaho'olawe 环顶点数 {len(ring)}；真值点在该环内 {len(hit)} 个")
    print(f"    → 若判据是 entry_allowed=false，这些点会产出 restricted_area_entry 事件")
else:
    print("    ⚠ 没找到 Kaho'olawe，规则可能没写进去")
