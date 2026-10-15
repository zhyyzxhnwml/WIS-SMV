# -*- coding: utf-8 -*-
"""拿系统的区域违规判定去测**真实官方禁渔区真值**。

【为什么必须做这一步】
  `build_real_violations.py` 只是把真值算出来了（渔船进入了法定禁捕区），
  但还没有证明"系统能不能判出来"。有真值、没有系统输出，等于没做实验。
  本脚本把 NOAA 官方边界喂给系统的区域引擎，跑真实 AIS，再和真值对拍。

【真值定义（三条都是法规事实，不经过我们的检测代码）】
  1) 边界：NOAA MPA Inventory，Fish_Rstr = 商业捕捞禁止；
  2) 船型：AIS 报文里的 VesselType ∈ {30, 1001, 1002}（渔业）；
  3) 位置：MarineCadastre 实测 AIS 点落在上述边界内。

【本脚本刻意保留的一处"不匹配"，它本身就是结论】
  系统的 `illegal_fishing` 判据是「区域内 + 船速 ≤ low_speed_enter_knots(默认 3 节)
  + 持续 ≥5 分钟」（见 ais_accelerator/detect.py:451-465）。
  而法定违规是「**商业渔船**在该区域内」——**船型 + 在场**，与速度无关。
  实测真值里那两条船是以 5.4 / 8.6 节**过境**的，因此按现有判据应当漏报。
  不把这条差异抹平（比如偷偷调高 3 节的阈值）是刻意的：论文要报的正是
  "现有判据把'捕捞'等同于'低速'，在船型可得的今天这个等价关系不成立"。

【区域几何】
  系统只支持**单环**边界（rules.py:93 调 point_in_polygon，无洞、无 MultiPolygon）。
  NOAA 给的是 MultiPolygon，这里取面积最大的那个外环；顶点用
  shapely.simplify 抽稀（系统对每个点做 O(n) 射线法，顶点太多会拖垮全量扫描）。
  抽稀容差写进输出，便于复核。

用法：
  python experiments/paper2/eval_real_zones.py                 # 两个违规日 + 对照日
  python experiments/paper2/eval_real_zones.py --days 2018-02-19
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"E:\AIS")
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ZONES = ROOT / "data/public/zones"
AIS_DIR = ROOT / "data/public/marinecadastre/hawaii"
TMP = ROOT / "data/logs/paper2"
OUT = ROOT / "data/logs"

# 真值里出现的两个违规日；再配几个对照日（同日历、无违规则预期 0 报）
GT_DAYS = ["2018-02-19", "2018-07-22"]
CTRL_DAYS = ["2018-02-18", "2018-07-21"]

SIMPLIFY_DEG = 0.0005   # ~55 m；Kaho'olawe 201 km² 用这个容差足够贴合海岸线


def build_regulations(state: str, out_json: Path) -> tuple[list[dict], dict]:
    """把 NOAA 官方边界转成系统的 zones 结构（单环、禁渔不禁入）。"""
    from shapely.geometry import shape as shp_shape

    src = json.loads((ZONES / f"noaa_mpa_{state}.geojson").read_text(encoding="utf-8"))
    hard = {s["site_name"] for s in json.loads(
        (ZONES / f"noaa_mpa_{state}_hardban.json").read_text(encoding="utf-8"))["sites"]}

    zones: list[dict] = []
    info: dict = {}
    for f in src.get("features", []):
        props = f["properties"]
        name = props.get("Site_Name")
        if name not in hard or not f.get("geometry"):
            continue
        geom = shp_shape(f["geometry"])
        if geom.geom_type == "MultiPolygon":
            polys = sorted(geom.geoms, key=lambda p: p.area, reverse=True)
            geom = polys[0]
        elif geom.geom_type != "Polygon":
            continue
        simple = geom.simplify(SIMPLIFY_DEG, preserve_topology=True)
        if simple.is_empty or simple.geom_type != "Polygon":
            simple = geom
        ring = [[round(x, 6), round(y, 6)] for x, y in simple.exterior.coords]
        if len(ring) < 4:
            continue
        # 禁令口径直接映射 NOAA 的 Fish_Rstr 原文，不做主观解读：
        #   全禁 → scope=all（任何渔船都不许作业）
        #   只禁商业 → scope=commercial（判据用 commercial_fishing_vessel_codes）
        rstr = str(props.get("Fish_Rstr") or "")
        scope = "commercial" if rstr.startswith("Commercial Fishing Prohibited") else "all"
        zones.append({
            "id": f"noaa_{props.get('Site_ID')}",
            "name": name,
            "type": "fishing_protected",
            # 这些区**都不禁止进入**（只禁捕），所以 entry_allowed 保持 true，
            # 否则会把过境船判成"闯禁区"，那是判错了条款。
            "restrictions": {"fishing_allowed": False,
                             "fishing_ban_scope": scope,
                             "entry_allowed": True,
                             "max_speed_knots": None},
            "time_rules": [],   # 该保护区的禁捕是全年，不是季节性禁渔期
            "boundary": ring,
        })
        info[name] = {"vertices_raw": len(geom.exterior.coords),
                      "vertices_used": len(ring), "area_km2": props.get("AreaKm"),
                      "fish_rstr": props.get("Fish_Rstr")}

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps({
        "meta": {"description": "NOAA MPA Inventory 官方边界 → 系统 zones 结构",
                 "source": "NOAA MPA Inventory 2023 (ArcGIS FeatureServer)",
                 "simplify_deg": SIMPLIFY_DEG,
                 "note": "仅供论文基准使用；不改动线上 regulations.json"},
        "zones": zones, "ship_types": {},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return zones, info


def convert_day(day: str, out_dir: Path) -> tuple[Path | None, int]:
    """MarineCadastre gz → 管线 7 列格式（时间写成无歧义的 ISO）。"""
    src = AIS_DIR / f"{day}.csv.gz"
    if not src.exists():
        return None, 0
    out = out_dir / f"_{day}_converted.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with gzip.open(src, "rt", encoding="utf-8", newline="") as f, \
            open(out, "w", encoding="utf-8", newline="") as g:
        rd, wr = csv.DictReader(f), csv.writer(g)
        # 船型列必须带上：`illegal_fishing` 的法定判据是"商业渔船在该水域内"，
        # 靠的是 VesselType，不是速度。丢了它禁渔判定会静默退回速度代理。
        wr.writerow(["mmsi", "time", "lon", "lat", "course", "heading", "speed",
                     "vessel_type"])
        for r in rd:
            try:
                t = datetime.fromisoformat(str(r["BaseDateTime"]).replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                wr.writerow([str(r["MMSI"]).strip(), t.strftime("%Y-%m-%d %H:%M:%S"),
                             r["LON"], r["LAT"], r.get("COG") or 0,
                             r.get("Heading") or 511, r.get("SOG") or 0,
                             (r.get("VesselType") or "").strip()])
                n += 1
            except (KeyError, ValueError):
                continue
    return out, n


def run_pipeline(csv_path: Path, reg_path: Path):
    """用指定区域规则跑加速器，返回 {mmsi: [事件]}。"""
    from ais_accelerator.config import AcceleratorConfig
    from ais_accelerator.pipeline import AcceleratorPipeline

    cfg = AcceleratorConfig(regulations_path=reg_path)
    pipe = AcceleratorPipeline(config=cfg)
    tracks, metrics = pipe.annotate_file(str(csv_path))
    per: dict[str, list[dict]] = {}
    for t in tracks:
        evs = []
        for e in (getattr(t, "events", None) or []):
            ctype = getattr(getattr(e, "candidate_type", None), "value", "")
            if ctype not in ("illegal_fishing", "restricted_area_entry"):
                continue
            feats = dict(getattr(e, "features", None) or {})
            evs.append({"type": ctype,
                        "zone": getattr(e, "zone_name", None),
                        "start": e.start_time.isoformat(),
                        "end": e.end_time.isoformat(),
                        "features": feats})
        if evs:
            per[str(t.mmsi)] = evs
    return per, metrics


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="HI")
    ap.add_argument("--days", nargs="*", default=None,
                    help="只跑指定日期；默认跑两个违规日 + 两个对照日")
    ap.add_argument("--out", default="data/logs/real_zone_eval.json")
    args = ap.parse_args()

    days = args.days or (GT_DAYS + CTRL_DAYS)
    gt_days = set(GT_DAYS)

    reg_path = TMP / "regulations_hawaii.json"
    zones, zinfo = build_regulations(args.state, reg_path)
    print(f"区域规则 → {reg_path}")
    print(f"写入 {len(zones)} 个官方禁渔区（单环、禁渔不禁入）")
    print(f"\n{'区域':<46}{'原始顶点':>9}{'抽稀后':>8}{'km²':>10}")
    print("-" * 76)
    for name, i in sorted(zinfo.items(), key=lambda kv: -(kv[1]["area_km2"] or 0)):
        print(f"{str(name)[:45]:<46}{i['vertices_raw']:>9}{i['vertices_used']:>8}"
              f"{(i['area_km2'] or 0):>10.2f}")

    gt = json.loads((OUT / "real_violations.json").read_text(encoding="utf-8"))
    gt_pairs = {(v["mmsi"], v["zone"]) for v in gt["violations"]}
    # 每条真值违规只属于**它自己那一天**。早先把两天合并成同一个期望集合，
    # 于是每天都会被判"漏报"另一天的那条船 —— 那不是漏报，是那天它根本不在数据里。
    gt_by_day: dict[str, set[str]] = {}
    for v in gt["violations"]:
        gt_by_day.setdefault(v["t0"][:10], set()).add(v["mmsi"])
    print(f"\n真值（真实违规）共 {len(gt_pairs)} 条：")
    for m, z in sorted(gt_pairs):
        v = next(x for x in gt["violations"] if x["mmsi"] == m)
        print(f"   {m}  {v['vessel']}  @ {z}   日期 {v['t0'][:10]}")

    results: dict = {"regulations": str(reg_path), "zones": zinfo, "days": {}}
    print(f"\n{'='*96}")
    print(f"{'日期':<14}{'船数':>7}{'事件船':>8}{'命中真值':>10}   说明")
    print("-" * 96)

    total_hit = 0
    for day in days:
        csv_path, nrows = convert_day(day, TMP / "conv")
        if csv_path is None:
            print(f"{day:<14}{'缺失':>7}")
            continue
        t0 = time.perf_counter()
        per, metrics = run_pipeline(csv_path, reg_path)
        dt = time.perf_counter() - t0
        hit_msis = {m for m in per if any(
            (m, e["zone"]) in gt_pairs for e in per[m])}
        expected = gt_by_day.get(day, set())
        if day in gt_days:
            missed, extra = expected - hit_msis, hit_msis - expected
            parts = [f"真值 {len(expected)} 命中 {len(hit_msis & expected)}"]
            if missed:
                parts.append(f"漏报 {','.join(sorted(missed))}")
            if extra:
                parts.append(f"误报 {','.join(sorted(extra))}")
            note = "；".join(parts)
            total_hit += len(hit_msis & expected)
        else:
            # 对照日真值为 0，任何事件船都是误报
            note = ("对照日（真值 0）" if not hit_msis
                    else f"对照日误报 {len(hit_msis)}")
        print(f"{day:<14}{metrics.ship_count:>7}{len(per):>8}{len(hit_msis):>10}   {note}  [{dt:.0f}s]")
        results["days"][day] = {
            "n_ships": metrics.ship_count, "n_rows": nrows,
            "ships_with_zone_events": len(per), "hits": sorted(hit_msis),
            "expected": sorted(expected), "events": per,
        }

    print("-" * 96)
    print(f"真值命中合计 {total_hit} / {len(gt_pairs)}")

    out = ROOT / args.out
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
