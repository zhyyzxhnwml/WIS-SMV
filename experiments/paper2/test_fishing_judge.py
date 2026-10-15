# -*- coding: utf-8 -*-
"""禁渔判据的回归测试：船型判据 + 速度代理回退 + 反向误报消除。

【为什么要有这个测试】
  `illegal_fishing` 的判据刚从「速度代理」换成「船型」，这属于核心变更，
  只靠跑一次真实数据（2/2）不足以锁住行为 —— 真实数据里没有覆盖到的分支
  （船型是非渔业、船型缺失、全禁 vs 只禁商业）必须单独钉住。

【锁定以下 5 条行为】
  1. 商业渔船(1001) 在"只禁商业捕捞"区内 → 报，judge=vessel_type，confirmed
  2. 通用渔船(30)   在同一区内             → 报，judge=vessel_type_suspect，suspect
     （30 分不出商业/休闲，不能静默丢弃，降一档交人工确认）
  3. 货船(70)       在同一区内**低速锚泊** → **不报**
     （这是换成船型判据后**消掉的误报**：旧速度判据会把任何 ≤3 节的船当捕捞）
  4. 船型缺失 + 低速(≤3节) + 全禁区 → 报，judge=low_speed（回退路径仍有效）
  5. 船型缺失 + 高速(8节)  + 全禁区 → 不报

用法：
  python experiments/paper2/test_fishing_judge.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(r"E:\AIS")
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ais_accelerator.config import AcceleratorConfig            # noqa: E402
from ais_accelerator.detect import _detect_zone_events          # noqa: E402
from ais_accelerator.geometry import point_in_polygon           # noqa: E402
from ais_accelerator.models import AISPoint, TrajectorySegment  # noqa: E402

# 一个 0.2°×0.2° 的方形测试区（够小，点不会跑到外面）
ZONE = {
    "id": "test_zone",
    "name": "测试禁渔区",
    "restrictions": {"fishing_allowed": False, "entry_allowed": True},
    "time_rules": [],
    "boundary": [[120.0, 25.0], [120.2, 25.0], [120.2, 25.2], [120.0, 25.2], [120.0, 25.0]],
}


class FakeRules:
    """只实现检测器用到的那一个方法。"""

    version = "test"

    def __init__(self, zone: dict):
        self.zone = zone

    def zones_at(self, lat: float, lon: float) -> list[dict]:
        ring = self.zone["boundary"]
        return [self.zone] if point_in_polygon(lon, lat, ring) else []


def make_segment(vtype: str | None, speed: float, n: int = 10) -> TrajectorySegment:
    t0 = datetime(2024, 6, 1, 8, 0, tzinfo=timezone.utc)
    pts = [
        AISPoint(mmsi="123456789", timestamp=t0 + timedelta(minutes=i),
                 lat=25.1, lon=120.1, speed=speed, course=90.0,
                 vessel_type=vtype)
        for i in range(n)
    ]
    return TrajectorySegment(segment_id="seg1", mmsi="123456789",
                             start_time=pts[0].timestamp, end_time=pts[-1].timestamp,
                             points=pts)


def run(scope: str, vtype: str | None, speed: float) -> list[dict]:
    zone = {**ZONE, "restrictions": {**ZONE["restrictions"],
                                     "fishing_ban_scope": scope}}
    cfg = AcceleratorConfig()
    evs = _detect_zone_events(make_segment(vtype, speed), FakeRules(zone), cfg)
    return [e.features for e in evs if e.candidate_type.value == "illegal_fishing"]


def main() -> int:
    cases = [
        # (说明, scope, 船型, 速度, 期望报出?, 期望 judge)
        ("商业渔船(1001) 出现在只禁商业的区内", "commercial", "1001", 6.0, True, "vessel_type"),
        ("通用渔船(30) 出现在只禁商业的区内", "commercial", "30", 8.6, True, "vessel_type_suspect"),
        ("货船(70) 在只禁商业的区内低速锚泊", "commercial", "70", 0.5, False, None),
        ("船型缺失 + 低速 + 全禁区", "all", None, 2.0, True, "low_speed"),
        ("船型缺失 + 高速 + 全禁区", "all", None, 8.0, False, None),
        ("商业渔船(1001) 在全禁区", "all", "1001", 9.0, True, "vessel_type"),
        ("货船(70) 在全禁区低速", "all", "70", 1.0, False, None),
    ]

    bad = 0
    print(f"{'#':<3}{'用例':<38}{'报出':<7}{'judge':<22}{'结论'}")
    print("-" * 92)
    for i, (desc, scope, vt, spd, want, want_judge) in enumerate(cases, 1):
        feats = run(scope, vt, spd)
        got = bool(feats)
        judge = feats[0].get("judge") if feats else "-"
        ok = (got == want) and (not want or judge == want_judge)
        bad += 0 if ok else 1
        vt_disp = vt if vt else "None"
        print(f"{i:<3}{desc:<38}{str(got):<7}{str(judge):<22}"
              f"{'通过' if ok else '**失败**'}  (船型={vt_disp} {spd}节)")
    print("-" * 92)
    if bad:
        print(f"{bad} 个用例失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
