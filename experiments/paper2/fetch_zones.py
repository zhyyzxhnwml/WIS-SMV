# -*- coding: utf-8 -*-
"""下载**真实官方法定边界**，作为违规判定的真值来源。

【为什么需要这个】
  论文要证明"系统能判违规"，就必须有一份**不是我们自己造出来的**真值。
  现有的注入集是用本仓的规则语义生成的，存在构造相关性（L1 在它上面 100%）。
  公开的 COLREGs（让路/追越）违规标注数据集并不存在，但**区域类违规**
  可以拿到货真价实的官方边界 —— 边界是法规条文的一部分，不是我们的推断。

【数据源】
  1) NOAA 海洋保护区清单（MPA Inventory 2023）
     ArcGIS FeatureServer，公开无鉴权，可直接取 GeoJSON 多边形。
     https://services2.arcgis.com/C8EMgrsFcRFL6LrL/ArcGIS/rest/services/
     NOAA_MPA_Inventory_2023/FeatureServer/0
     字段 `Fish_Rstr` 给出禁渔强度，其中
       "Commercial and Recreational Fishing Prohibited" → 商业捕捞全禁
       "Commercial Fishing Prohibited and Recreational Fishing Restricted" → 商业捕捞禁
     两者才是可用于"违规捕捞"判定的硬边界；其余（Restricted / No Site Restrictions）
     只是限制或有条件开放，拿来当违规真值会造出大量假正例。

  2) 福建省机动底拖网作业禁渔区
     已在 `benchmark/zones/fujian_trawling_ban.json`（省海洋与渔业局通告），
     本脚本只做校验，不重复下载。

【缓存】落盘到 data/public/zones/，已存在且非空则跳过。

用法：
  python experiments/paper2/fetch_zones.py --state HI
  python experiments/paper2/fetch_zones.py --state FL
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(r"E:\AIS")
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SERVICE = ("https://services2.arcgis.com/C8EMgrsFcRFL6LrL/ArcGIS/rest/services/"
           "NOAA_MPA_Inventory_2023/FeatureServer/0")
OUT = ROOT / "data/public/zones"

# 只有这两种强度能承载"违规捕捞"的硬判定
HARD_BAN = (
    "Commercial and Recreational Fishing Prohibited",
    "Commercial Fishing Prohibited and Recreational Fishing Restricted",
)


def query(params: dict) -> dict:
    url = f"{SERVICE}/query?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "ais-violation-research/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def bbox_of(geom: dict) -> tuple[float, float, float, float]:
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
    return min(xs), min(ys), max(xs), max(ys)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="HI", help="美国州代码；留空取全国")
    ap.add_argument("--out-dir", default=str(OUT))
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"noaa_mpa_{args.state or 'ALL'}"
    geo_path = out_dir / f"{stem}.geojson"
    if geo_path.exists() and geo_path.stat().st_size > 200:
        print(f"已存在，跳过：{geo_path}")
    else:
        where = "1=1" if not args.state else f"State='{args.state}'"
        print(f"下载 NOAA MPA（where={where}）...")
        payload = query({
            "where": where,
            "outFields": "Site_ID,Site_Name,State,Gov_Level,Prot_Lvl,Fish_Rstr,"
                         "IUCNcat,AreaKm,Estab_Yr,Longitude,Latitude",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "geojson",
            "resultRecordCount": "2000",
        })
        feats = payload.get("features", [])
        print(f"  取到 {len(feats)} 个要素")
        geo_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"  → {geo_path}")

    payload = json.loads(geo_path.read_text(encoding="utf-8"))
    feats = [f for f in payload.get("features", []) if f.get("geometry")]
    print(f"\n共 {len(feats)} 个带几何的要素")

    hard = [f for f in feats if (f["properties"].get("Fish_Rstr") or "").strip() in HARD_BAN]
    print(f"其中**商业捕捞禁止**（可作违规硬真值）：{len(hard)}")
    print()
    print(f"{'Site_Name':<44}{'IUCN':<6}{'km2':>9}{'Year':>6}  bbox(lon,lat)")
    print("-" * 118)
    rows = []
    for f in hard:
        p = f["properties"]
        b = bbox_of(f["geometry"])
        rows.append((p, b))
    rows.sort(key=lambda r: -(r[0].get("AreaKm") or 0))
    for p, b in rows[:25]:
        print(f"{str(p.get('Site_Name'))[:43]:<44}{str(p.get('IUCNcat')):<6}"
              f"{(p.get('AreaKm') or 0):>9.1f}{str(p.get('Estab_Yr')):>6}  "
              f"{b[0]:.3f},{b[1]:.3f} .. {b[2]:.3f},{b[3]:.3f}")

    idx = out_dir / f"{stem}_hardban.json"
    idx.write_text(json.dumps({
        "source": SERVICE,
        "state": args.state,
        "rule": "Fish_Rstr ∈ 商业捕捞禁止 → 该边界内的渔业作业构成违规",
        "n_features": len(feats),
        "n_hard_ban": len(hard),
        "sites": [{"site_id": p.get("Site_ID"), "site_name": p.get("Site_Name"),
                   "state": p.get("State"), "fish_rstr": p.get("Fish_Rstr"),
                   "iucn": p.get("IUCNcat"), "area_km2": p.get("AreaKm"),
                   "estab_yr": p.get("Estab_Yr"), "bbox": bbox_of(f["geometry"])}
                  for p, f in ((r[0], next(x for x in hard if x["properties"] is r[0]))
                               for r in rows)],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n索引 → {idx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
