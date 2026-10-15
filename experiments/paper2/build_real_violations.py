# -*- coding: utf-8 -*-
"""在**真实官方禁渔边界**上构建违规评测集（不是合成的）。

【为什么这是"真值"而不是"我们算出来的"】
  违规 = 渔船进入法定禁止捕捞的水域。
  两个前提都是**法规事实**，不是本系统的推断：
    1) 边界来自 NOAA MPA Inventory（法定管理区多边形），`Fish_Rstr` =
       "Commercial (and Recreational) Fishing Prohibited" 即商业捕捞全禁；
    2) 船型来自 AIS 报文的 `VesselType`，Fishing = {30, 1001, 1002}
       （30=Fishing，1001=Commercial Fishing Vessel，1002=Fish Processing）。
  所以"渔船出现在禁渔区内"这一条**不依赖我们任何检测代码**，
  可以直接和系统的 `illegal_fishing` / `restricted_area_entry` 判定对拍。

【它与注入集的根本差别（决定它能证明什么）】
  注入集：场景由本仓规则语义生成 → L1 在上面 100% 是**部分构造**出来的。
  本集合：边界是政府公告、船型是船舶自己广播的、轨迹是实测的 →
  系统在它上面得多少分，是**转移性**的证据，不是构造自证。

【两个必须诚实标注的口径】
  · 判据只到"渔船在禁渔区内"，不含"正在作业"。浮标/锚泊/过境也会有正例。
    这是**偏严的**（把过境也算违规），论文里要写明；本脚本同时输出
    "区内低速点数"作为可选的更严子集，供正文与敏感性分析各用一套。
  · 点在边界上/坐标系上，用 WGS84 经纬度直接做点多边形，未做投影；
    MPA 尺度 0.1–2000 km²，在中纬度的等距圆柱误差 < 0.1%，可忽略。

【跨日期变更线的坑】
  Papahanaumokuakea 的几何跨 ±180°，GeoJSON 里 bbox 会变成 -180..180，
  点入多边形会假命中。本脚本对 bbox 宽度 > 20° 的多边形做显式标注并跳过，
  它们也远在本实验的 AIS 覆盖（瓦胡岛 -158°）之外。

用法：
  python experiments/paper2/build_real_violations.py
  python experiments/paper2/build_real_violations.py --state HI --min-speed 0
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"E:\AIS")
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ZONES = ROOT / "data/public/zones"
AIS_DIR = ROOT / "data/public/marinecadastre"
OUT = ROOT / "data/logs"

# AIS 船型编码里属于"渔业"的取值（见 实验数据/HawaiiCoast_GT_GitHub/
# vessel_type_codes_2018.csv：30=Fishing, 1001=Commercial Fishing Vessel,
# 1002=Fish Processing Vessel）
FISHING_TYPES = {"30", "1001", "1002"}

# 边界框宽度超过这个度数 = 跨日期变更线，跳过（避免假命中）
MAX_BBOX_SPAN_DEG = 20.0


def load_polys(state: str) -> tuple[list[dict], list[dict]]:
    p = ZONES / f"noaa_mpa_{state}.geojson"
    payload = json.loads(p.read_text(encoding="utf-8"))
    hard_names = {
        s["site_name"] for s in
        (json.loads((ZONES / f"noaa_mpa_{state}_hardban.json").read_text(encoding="utf-8"))
         ["sites"])
    }
    keep: list[dict] = []
    skipped: list[dict] = []
    for f in payload.get("features", []):
        geom = f.get("geometry")
        if not geom:
            continue
        props = f["properties"]
        if (props.get("Site_Name") or "") not in hard_names:
            continue
        polys = (geom["coordinates"] if geom["type"] == "MultiPolygon"
                 else [geom["coordinates"]])
        xs: list[float] = []
        ys: list[float] = []

        def walk(c) -> None:
            if isinstance(c[0], (int, float)):
                xs.append(float(c[0]))
                ys.append(float(c[1]))
            else:
                for child in c:
                    walk(child)

        walk(geom["coordinates"])
        bb = (min(xs), min(ys), max(xs), max(ys))
        rec = {"name": props.get("Site_Name"), "fish_rstr": props.get("Fish_Rstr"),
               "area_km2": props.get("AreaKm"), "bbox": bb, "polys": polys}
        if bb[2] - bb[0] > MAX_BBOX_SPAN_DEG:
            skipped.append(rec)
        else:
            keep.append(rec)
    return keep, skipped


def in_ring(x: float, y: float, ring: list) -> bool:
    """射线法点在多边形内。ring 为 [[lon,lat], ...]。"""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y):
            xint = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < xint:
                inside = not inside
        j = i
    return inside


def in_poly(x: float, y: float, polys: list) -> bool:
    """polys 为 [ring, ...]（首个外环，其余为洞）。"""
    if not polys:
        return False
    if not in_ring(x, y, polys[0]):
        return False
    for hole in polys[1:]:
        if in_ring(x, y, hole):
            return False
    return True


def in_zone(x: float, y: float, zones: list[dict]) -> dict | None:
    for z in zones:
        b = z["bbox"]
        if not (b[0] <= x <= b[2] and b[1] <= y <= b[3]):
            continue
        for poly in z["polys"]:
            if in_poly(x, y, poly):
                return z
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="HI")
    ap.add_argument("--ais-dir", default=str(AIS_DIR / "hawaii"))
    ap.add_argument("--out", default="data/logs/real_violations.json")
    args = ap.parse_args()

    zones, skipped = load_polys(args.state)
    print(f"商业捕捞禁止边界：{len(zones)} 个可判（跳过跨日期变更线 {len(skipped)} 个）")
    for z in sorted(zones, key=lambda z: -(z["area_km2"] or 0)):
        b = z["bbox"]
        print(f"   {str(z['name'])[:46]:<48}{(z['area_km2'] or 0):>9.2f} km²  "
              f"{b[0]:.3f},{b[1]:.3f}..{b[2]:.3f},{b[3]:.3f}")
    if skipped:
        print(f"   跳过：{', '.join(str(s['name'])[:30] for s in skipped)}")

    files = sorted(Path(args.ais_dir).glob("*.csv.gz"))
    print(f"\nAIS 天数：{len(files)}")

    # 每个 (mmsi, 区域) 的命中点数、最低速、时间范围、船名/船型
    hits: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"n": 0, "n_moving": 0, "min_sog": 9e9, "max_sog": -1.0,
                 "t0": None, "t1": None, "vessel": "", "vtype": ""})
    n_pts = 0
    n_fish_pts = 0
    fish_in_zone_pts = 0
    days_with_hit: set[str] = set()

    for fp in files:
        day = fp.name.replace(".csv.gz", "")
        try:
            with gzip.open(fp, "rt", encoding="utf-8", errors="replace") as fh:
                for r in csv.DictReader(fh):
                    try:
                        lon = float(r["LON"]); lat = float(r["LAT"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    n_pts += 1
                    vt = (r.get("VesselType") or "").strip()
                    is_fish = vt in FISHING_TYPES
                    if is_fish:
                        n_fish_pts += 1
                    # 先用所有点的 bbox 预筛，避免对每个点遍历全部多边形
                    if not any(z["bbox"][0] <= lon <= z["bbox"][2]
                               and z["bbox"][1] <= lat <= z["bbox"][3] for z in zones):
                        continue
                    z = in_zone(lon, lat, zones)
                    if z is None:
                        continue
                    fish_in_zone_pts += 1
                    if not is_fish:
                        continue
                    try:
                        sog = float(r.get("SOG") or 0.0)
                    except ValueError:
                        sog = 0.0
                    key = (str(r.get("MMSI") or ""), str(z["name"]))
                    h = hits[key]
                    h["n"] += 1
                    if sog > 0.5:
                        h["n_moving"] += 1
                    h["min_sog"] = min(h["min_sog"], sog)
                    h["max_sog"] = max(h["max_sog"], sog)
                    ts = r.get("BaseDateTime")
                    if ts:
                        if h["t0"] is None or ts < h["t0"]:
                            h["t0"] = ts
                        if h["t1"] is None or ts > h["t1"]:
                            h["t1"] = ts
                    h["vessel"] = r.get("VesselName") or h["vessel"]
                    h["vtype"] = vt or h["vtype"]
                    days_with_hit.add(day)
        except (OSError, EOFError) as exc:                                # noqa: BLE001
            print(f"  ⚠ {fp.name} 读取失败：{exc}")

    print(f"\n扫描点数 {n_pts:,}（其中渔船 {n_fish_pts:,} = "
          f"{n_fish_pts / max(n_pts, 1) * 100:.1f}%）")
    print(f"落在禁渔区内的点数 {fish_in_zone_pts:,}")
    print(f"其中渔船的点数 = {sum(h['n'] for h in hits.values()):,}")

    rows = []
    for (mmsi, zone), h in hits.items():
        rows.append({"mmsi": mmsi, "zone": zone, "vessel": h["vessel"],
                     "vessel_type": h["vtype"], "n_points": h["n"],
                     "n_moving": h["n_moving"],
                     "min_sog": round(h["min_sog"], 2) if h["min_sog"] < 9e9 else None,
                     "max_sog": round(h["max_sog"], 2) if h["max_sog"] >= 0 else None,
                     "t0": h["t0"], "t1": h["t1"]})
    rows.sort(key=lambda r: -r["n_points"])
    print(f"\n**渔船进入禁渔区的 (船, 区域) 组合：{len(rows)}**")
    print(f"涉及天数：{len(days_with_hit)}")
    print(f"\n{'MMSI':<12}{'船名':<20}{'船型':<6}{'点':>6}{'移动':>6}{'最低速':>8}  区域")
    print("-" * 110)
    for r in rows[:30]:
        print(f"{r['mmsi']:<12}{str(r['vessel'])[:19]:<20}{r['vessel_type']:<6}"
              f"{r['n_points']:>6}{r['n_moving']:>6}"
              f"{(r['min_sog'] if r['min_sog'] is not None else -1):>8.2f}  "
              f"{str(r['zone'])[:44]}")

    out = ROOT / args.out
    out.write_text(json.dumps({
        "source": "NOAA MPA Inventory 2023 (ArcGIS FeatureServer, 公开无鉴权)"
                  " + MarineCadastre AIS (VesselType)",
        "state": args.state,
        "rule": "渔船(VesselType∈{30,1001,1002})出现在 Fish_Rstr=商业捕捞禁止 的法定区域内",
        "n_ais_days": len(files),
        "n_points": n_pts,
        "n_fishing_points": n_fish_pts,
        "n_zone_points": fish_in_zone_pts,
        "zones": [{"name": z["name"], "area_km2": z["area_km2"], "bbox": z["bbox"]}
                  for z in zones],
        "violations": rows,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
