"""
scripts/backfill_violations.py — 存量违规记录规范化回填
=========================================================

给 detection_results 里 violation_detected=1 的存量记录补上：
  - violation_label   中文标签（violation_catalog 归一化）
  - regulation_ref    法规引用（锚定条款 + 真实余弦相关度；检索不到用兜底文本）
  - violation_event   可读违规事件（"检测出xxx，与《法规》第X条相关度x%"）

用法：
  python scripts/backfill_violations.py
"""
# -*- coding: utf-8 -*-
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pg_db  # noqa: E402
from ais_accelerator.violation_catalog import (  # noqa: E402
    normalize_violation_code, describe, FALLBACK_REG, build_violation_event,
)


def main() -> int:
    # 1) 确保新列存在
    conn = pg_db.connect()
    conn.execute("ALTER TABLE detection_results ADD COLUMN IF NOT EXISTS violation_label TEXT DEFAULT ''")
    conn.execute("ALTER TABLE detection_results ADD COLUMN IF NOT EXISTS violation_event TEXT DEFAULT ''")
    conn.commit()

    # 2) 法规检索器（Milvus 不可用时全程走兜底文本）
    try:
        from ais_accelerator.regulation_db import RegulationDB
        regdb = RegulationDB()
        reg_ok = regdb._available and regdb._has_collection()
    except Exception:
        regdb, reg_ok = None, False
    print("Milvus 法规库可用:", reg_ok)

    rows = conn.execute(
        "SELECT id, mmsi, violation_type, final_assessment, regulation_ref "
        "FROM detection_results WHERE violation_detected = 1"
    ).fetchall()
    print("待回填违规记录:", len(rows))

    n_ref = 0
    for r in rows:
        raw = (r["violation_type"] or "").strip()
        code = normalize_violation_code(raw)
        info = describe(code)
        arts, best, reg_ref = [], None, ""
        if reg_ok and raw:
            try:
                arts = regdb.search_by_violation(raw, None, top_k=3)
            except Exception:
                arts = []
        best = arts[0] if arts else None
        if arts:
            refs = []
            for a in arts:
                name = a.get("source", "")
                num = a.get("article_number", "")
                title = (a.get("title") or "").strip()
                sim = a.get("similarity")
                ref = name
                if num and num != "0":
                    ref += f" 第{num}条"
                if title:
                    ref += f"《{title}》"
                if sim is not None:
                    ref += f"（相关度{sim:.0%}）"
                refs.append(ref)
            reg_ref = "；".join(refs[:3])
            n_ref += 1
        else:
            reg_ref = FALLBACK_REG.get(code, FALLBACK_REG.get("unknown", ""))
        event = build_violation_event(raw, r["final_assessment"] or "", reg_ref, best)
        conn.execute(
            "UPDATE detection_results SET violation_label=?, violation_event=?, regulation_ref=? WHERE id=?",
            (info["label"], event, reg_ref, r["id"]),
        )
    conn.commit()
    conn.close()
    print("回填完成。法规引用非空记录:", n_ref, "/", len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
